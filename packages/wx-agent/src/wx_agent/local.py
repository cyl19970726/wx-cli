"""Shared local helpers: bounded subprocess calls and private JSON state files."""
import errno
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import tempfile
import time

MAX_OUTPUT = 16 * 1024 * 1024
MAX_REPLY = 64 * 1024

# File names written by the retired single-group runner. They are only read to
# prove that the old runner is stopped before its registry entry is retired.
LEGACY_PID_NAME = "channel-agent-controller.json"
LEGACY_STATE_NAME = "channel-agent.json"
LEGACY_LEASE_NAME = "channel-agent-interactive.json"


class CommandError(Exception):
    """A child process failed, timed out or produced unusable output."""


class StateError(Exception):
    """A private local state file is unsafe or malformed."""


def reply_request_id(channel_id, server_id):
    if not isinstance(channel_id, str) or type(server_id) is not int or server_id <= 0:
        raise CommandError("stable positive server ID required")
    return "reply-" + hashlib.sha256(f"{channel_id}:{server_id}".encode()).hexdigest()


def command(argv, data=None, timeout=60, limit=MAX_OUTPUT):
    # stdout is capped while reading, including for an untrusted child process.
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    payload = b"" if data is None else data.encode("utf-8")
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    assert proc.stdin and proc.stdout
    os.set_blocking(proc.stdin.fileno(), False)
    os.set_blocking(proc.stdout.fileno(), False)
    selector.register(proc.stdout, selectors.EVENT_READ)
    if payload:
        selector.register(proc.stdin, selectors.EVENT_WRITE)
    else:
        proc.stdin.close()
    output = bytearray()
    written = 0
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandError(f"command timed out: {argv[0]}")
            for key, mask in selector.select(min(remaining, 0.2)):
                if key.fileobj is proc.stdin and mask & selectors.EVENT_WRITE:
                    try:
                        written += os.write(proc.stdin.fileno(), payload[written:written + 65536])
                    except BlockingIOError as exc:
                        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                            raise
                    except BrokenPipeError:
                        written = len(payload)
                    if written == len(payload):
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
                elif key.fileobj is proc.stdout and mask & selectors.EVENT_READ:
                    try:
                        chunk = os.read(proc.stdout.fileno(), min(65536, limit + 1 - len(output)))
                    except BlockingIOError as exc:
                        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                            raise
                        continue
                    output.extend(chunk)
                    if len(output) > limit:
                        raise CommandError(f"command output exceeded {limit} bytes: {argv[0]}")
                    if not chunk:
                        selector.unregister(proc.stdout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CommandError(f"command timed out: {argv[0]}")
        if proc.wait(timeout=remaining) != 0:
            raise CommandError(f"command failed: {argv[0]}")
        return output.decode("utf-8")
    except (CommandError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The child already exited; preserve the original failure.
        proc.wait()
        raise
    finally:
        selector.close()
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        if proc.stdout:
            proc.stdout.close()


def one_json(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise CommandError("invalid JSON command output") from exc
    if not isinstance(value, dict):
        raise CommandError("JSON object required")
    return value


def private(path, directory=False):
    info = path.lstat()
    expected = 0o700 if directory else 0o600
    right_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not right_kind or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected or
            (not directory and info.st_nlink != 1)):
        raise StateError("unsafe private path")


def read_json(path):
    private(path)
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (ValueError, UnicodeError) as exc:
        raise StateError("invalid private JSON") from exc
    if not isinstance(value, dict):
        raise StateError("JSON object required")
    return value


def atomic_json(path, value):
    private(path.parent, True)
    fd, temporary = tempfile.mkstemp(prefix=".wx-agent-", dir=path.parent)
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


def _read_optional(path):
    if not path.exists() and not path.is_symlink():
        return None
    return read_json(path)


def _process_start(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                                text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def legacy_runner_blockers(state_dir):
    """Return reasons why a retired single-group runner may still own ``state_dir``.

    A recorded process whose start time still matches is treated as alive even
    if its command line can no longer be confirmed: refusing is the safe side.
    """
    state_dir = Path(state_dir)
    reasons = []
    metadata = _read_optional(state_dir / LEGACY_PID_NAME)
    if isinstance(metadata, dict):
        started = _process_start(metadata.get("pid"))
        if started is not None and started == metadata.get("start_stamp"):
            reasons.append("legacy runner process is still alive")
    if _read_optional(state_dir / LEGACY_LEASE_NAME) is not None:
        reasons.append("legacy interactive lease is still held")
    state = _read_optional(state_dir / LEGACY_STATE_NAME)
    if isinstance(state, dict) and state.get("pending") is not None:
        reasons.append("legacy runner has a pending reply")
    return reasons
