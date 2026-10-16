#!/usr/bin/env python3
"""Local, bounded control for one configured single-group reply runner."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

RUNNER = Path(__file__).with_name("channel_agent.py").resolve()
PID_NAME = "channel-agent-controller.json"
LOCK_NAME = "channel-agent-controller.lock"
CONTROL_NAME = "channel-agent-control.json"
HEALTH_NAME = "channel-agent-health.json"
LOG_NAME = "channel-agent.log"
STARTED = []


class ControlError(Exception):
    pass


def private(path, directory=False):
    info = path.lstat()
    expected = 0o700 if directory else 0o600
    right_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not right_kind or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected or (not directory and info.st_nlink != 1):
        raise ControlError("unsafe private path")


def read_json(path):
    private(path)
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (ValueError, UnicodeError) as exc:
        raise ControlError("invalid private JSON") from exc
    if not isinstance(value, dict):
        raise ControlError("JSON object required")
    return value


def atomic_json(path, value):
    private(path.parent, True)
    fd, temporary = tempfile.mkstemp(prefix=".channel-agent-ctl-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            private(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def config(path, require_executables=True):
    if not path.is_absolute():
        raise ControlError("absolute --config path required")
    value = read_json(path)
    required = {"wx_cli", "state_dir", "consumer", "agent_command", "sender_driver"}
    optional = {"poll_seconds", "read_retries", "retry_base_seconds", "start_after"}
    if set(value) - required - optional or not required.issubset(value):
        raise ControlError("invalid config keys")
    for key in ("wx_cli", "state_dir", "sender_driver"):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise ControlError("absolute config paths required")
    if not isinstance(value["consumer"], str) or not value["consumer"].strip():
        raise ControlError("consumer required")
    agent = value["agent_command"]
    if not isinstance(agent, list) or not agent or not all(isinstance(item, str) for item in agent) or not Path(agent[0]).is_absolute():
        raise ControlError("absolute agent command required")
    if "start_after" in value and (type(value["start_after"]) is not int or value["start_after"] < 0):
        raise ControlError("invalid start_after")
    if "read_retries" in value and (type(value["read_retries"]) is not int or value["read_retries"] < 0):
        raise ControlError("invalid read_retries")
    for key in ("poll_seconds", "retry_base_seconds"):
        if key in value and (type(value[key]) not in (int, float) or not 0 < value[key] <= 3600):
            raise ControlError("invalid timing config")
    state_dir = Path(value["state_dir"])
    private(state_dir, True)
    if require_executables:
        executable = Path(value["wx_cli"])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ControlError("configured wx-cli unavailable")
        executable = Path(agent[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ControlError("agent executable unavailable")
    return value


def process_stamp(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        stamp = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, timeout=2, check=False)
        args = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if stamp.returncode or args.returncode:
        return None
    return stamp.stdout.strip(), args.stdout.strip()


def alive(metadata, state_dir):
    if not isinstance(metadata, dict) or metadata.get("version") != 1:
        return False
    pid = metadata.get("pid")
    current = process_stamp(pid)
    if not current or not current[0] or current[0] != metadata.get("start_stamp"):
        return False
    argv = current[1]
    return str(RUNNER) in argv and "--state-dir " + str(state_dir) in argv


def metadata_for(state_dir):
    path = state_dir / PID_NAME
    if not path.exists() and not path.is_symlink():
        return None
    return read_json(path)


def health_for(state_dir):
    path = state_dir / HEALTH_NAME
    if not path.exists() and not path.is_symlink():
        return None
    value = read_json(path)
    if value.get("version") != 1:
        raise ControlError("invalid health version")
    return {key: value.get(key) for key in ("stage", "retry_count", "last_processed_cursor", "failure_category", "terminal_reason", "exit_code", "updated_at")}


def runner_argv(settings):
    argv = [sys.executable, str(RUNNER), "--wx-cli", settings["wx_cli"], "--state-dir", settings["state_dir"],
            "--consumer", settings["consumer"], "--agent-command-json", json.dumps(settings["agent_command"]),
            "--sender-driver", settings["sender_driver"], "--read-retries", str(settings.get("read_retries", 0))]
    for field, option in (("poll_seconds", "--poll-seconds"), ("retry_base_seconds", "--retry-base-seconds"), ("start_after", "--start-after")):
        if field in settings:
            argv += [option, str(settings[field])]
    return argv


def write_control(state_dir, action):
    atomic_json(state_dir / CONTROL_NAME, {"version": 1, "action": action})


def open_log(state_dir):
    path = state_dir / LOG_NAME
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private(path)
        return os.fdopen(fd, "ab", buffering=0)
    except Exception:
        os.close(fd)
        raise


def start(settings):
    state_dir = Path(settings["state_dir"])
    existing = metadata_for(state_dir)
    if existing and alive(existing, state_dir):
        raise ControlError("runner already active")
    runner_lock = state_dir / "channel-agent.lock"
    lock_fd = os.open(runner_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private(runner_lock)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControlError("runner lock is busy") from exc
    finally:
        os.close(lock_fd)
    write_control(state_dir, "run")
    with open_log(state_dir) as log:
        process = subprocess.Popen(runner_argv(settings), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   cwd=str(state_dir), start_new_session=True, close_fds=True)
    stamp = None
    for _ in range(20):
        identity = process_stamp(process.pid)
        if identity and str(RUNNER) in identity[1]:
            stamp = identity[0]
            break
        if process.poll() is not None:
            break
        time.sleep(0.05)
    if not stamp:
        raise ControlError("runner exited during startup; inspect private log")
    atomic_json(state_dir / PID_NAME, {"version": 1, "pid": process.pid, "start_stamp": stamp})
    STARTED.append(process)
    return {"started": True, "pid": process.pid}


def status(settings):
    for process in STARTED[:]:
        if process.poll() is not None:
            STARTED.remove(process)
    state_dir = Path(settings["state_dir"])
    metadata = metadata_for(state_dir)
    is_alive = alive(metadata, state_dir)
    return {"alive": is_alive, "pid": metadata.get("pid") if is_alive else None,
            "health": health_for(state_dir), "stale_pid": bool(metadata and not is_alive)}


def action(settings, verb, wait_seconds):
    state_dir = Path(settings["state_dir"])
    metadata = metadata_for(state_dir)
    if not metadata or not alive(metadata, state_dir):
        raise ControlError("no verified active runner")
    write_control(state_dir, {"pause": "pause", "resume": "run", "stop": "stop"}[verb])
    if verb == "stop":
        deadline = time.monotonic() + wait_seconds
        while alive(metadata, state_dir) and time.monotonic() < deadline:
            time.sleep(0.1)
    result = status(settings)
    result["requested"] = verb
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="absolute path to private 0600 controller JSON")
    parser.add_argument("--wait-seconds", type=float, default=15, help="stop wait; never force-kills an in-flight send")
    parser.add_argument("command", choices=("run", "start", "status", "pause", "resume", "stop"))
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.wait_seconds <= 60:
            raise ControlError("wait-seconds must be 0..60")
        settings = config(args.config, require_executables=args.command in ("start", "run"))
        state_dir = Path(settings["state_dir"])
        lock = state_dir / LOCK_NAME
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            private(lock)
            fcntl.flock(fd, fcntl.LOCK_EX)
            if args.command == "status":
                result = status(settings)
            elif args.command in ("start", "run"):
                result = start(settings)
            else:
                result = action(settings, args.command, args.wait_seconds)
        finally:
            os.close(fd)
        print(json.dumps(result, separators=(",", ":")), flush=True)
        if args.command == "run":
            process = STARTED[-1]
            try:
                code = process.wait()
            except KeyboardInterrupt:
                write_control(state_dir, "stop")
                code = process.wait()
            print(json.dumps({"alive": False, "exit_code": code, "health": health_for(state_dir)}, separators=(",", ":")))
            return code
        return 0
    except (ControlError, OSError, subprocess.SubprocessError) as exc:
        print("wechat agent control: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
