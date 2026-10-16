#!/usr/bin/env python3
"""One bound test-group reply runner. External agent and sender are explicit executables.

The agent reads one JSON object on stdin and writes {"text": "..."} or
{"skip": true} on stdout. Group messages are data, never runner instructions.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

STATE_NAME = "channel-agent.json"
LOCK_NAME = "channel-agent.lock"
HEALTH_NAME = "channel-agent-health.json"
EVENTS_NAME = "channel-agent-events.jsonl"
CONTROL_NAME = "channel-agent-control.json"
MAX_HEALTH_EVENTS = 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
MAX_REPLY = 64 * 1024


class RunnerError(Exception):
    pass


def private(path, directory=False):
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or mode != (0o700 if directory else 0o600) or (not directory and info.st_nlink != 1):
        raise RunnerError(f"unsafe private state path: {path}")


def executable(path):
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RunnerError(f"absolute executable required: {path}")


def command(argv, data=None, timeout=60, limit=MAX_OUTPUT):
    # stdout is capped while reading, including for an untrusted agent process.
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    import selectors
    import time
    import signal
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
                raise RunnerError(f"command timed out: {argv[0]}")
            for key, mask in selector.select(min(remaining, 0.2)):
                if key.fileobj is proc.stdin and mask & selectors.EVENT_WRITE:
                    try:
                        written += os.write(proc.stdin.fileno(), payload[written:written + 65536])
                    except BrokenPipeError:
                        written = len(payload)
                    if written == len(payload):
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
                elif key.fileobj is proc.stdout and mask & selectors.EVENT_READ:
                    chunk = os.read(proc.stdout.fileno(), min(65536, limit + 1 - len(output)))
                    output.extend(chunk)
                    if len(output) > limit:
                        raise RunnerError(f"command output exceeded {limit} bytes: {argv[0]}")
                    if not chunk:
                        selector.unregister(proc.stdout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RunnerError(f"command timed out: {argv[0]}")
        if proc.wait(timeout=remaining) != 0:
            raise RunnerError(f"command failed: {argv[0]}")
        return output.decode("utf-8")
    except (RunnerError, subprocess.TimeoutExpired):
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
        raise RunnerError("invalid JSON command output") from exc
    if not isinstance(value, dict):
        raise RunnerError("JSON object required")
    return value


def save(path, value):
    private(path.parent, True)
    fd, temp = tempfile.mkstemp(prefix=".channel-agent-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            private(path)
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load(path):
    private(path)
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RunnerError("unsupported runner state")
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_health_event(path, record):
    private(path.parent, True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        private(path)
        if os.fstat(fd).st_size >= MAX_HEALTH_EVENTS:
            os.ftruncate(fd, 0)
        payload = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


class ControlledStop(Exception):
    pass


class Runner:
    def __init__(self, args):
        self.args = args
        self.state_path = args.state_dir / STATE_NAME
        self.channel = [str(args.wx_cli), "channel", "--state-dir", str(args.state_dir)]
        self.outbox = [str(args.wx_cli), "outbox", "--state-dir", str(args.state_dir)]
        self.health_path = args.state_dir / HEALTH_NAME
        self.events_path = args.state_dir / EVENTS_NAME
        self.control_path = args.state_dir / CONTROL_NAME
        self.last_cursor = 0
        self.retry_count = 0
        self.stage = "reading"
        self.deadline = None
        self.in_flight_send = False
        self.state = None
        self.resume_stage = "reading"

    def health(self, stage, failure_category=None, terminal_reason=None, exit_code=None):
        self.stage = stage
        record = {"version": 1, "stage": stage, "retry_count": self.retry_count,
                  "last_processed_cursor": self.last_cursor,
                  "failure_category": failure_category,
                  "terminal_reason": terminal_reason, "exit_code": exit_code,
                  "updated_at": utc_now()}
        save(self.health_path, record)
        append_health_event(self.events_path, record)

    def control(self):
        if not self.control_path.exists() and not self.control_path.is_symlink():
            return "run"
        private(self.control_path)
        if self.control_path.stat().st_size > 4096:
            raise RunnerError("invalid control size")
        try:
            value = json.loads(self.control_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise RunnerError("invalid control JSON") from exc
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("action") not in ("run", "pause", "stop") or set(value) != {"version", "action"}:
            raise RunnerError("invalid control action")
        return value["action"]

    def boundary(self):
        while True:
            action = self.control()
            if action == "stop":
                return "stop"
            if self.deadline is not None and time.monotonic() >= self.deadline:
                return "max_seconds"
            if action == "run":
                if self.stage == "paused":
                    self.health(self.resume_stage)
                return None
            if self.stage != "paused":
                self.resume_stage = self.stage
                self.health("paused")
            time.sleep(0.25)

    def delay(self, seconds):
        end = time.monotonic() + seconds
        while True:
            reason = self.boundary()
            if reason:
                return reason
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.25, remaining))

    def safe_command(self, base, *parts, data=None, retry_mode="read"):
        attempts = 0
        target_stage = self.stage if self.stage != "paused" else "reading"
        while True:
            if not self.in_flight_send:
                reason = self.boundary()
                if reason:
                    raise ControlledStop(reason)
            try:
                result = command(base + list(parts), data=data)
                if attempts:
                    self.health(target_stage)
                return result
            except (RunnerError, OSError, subprocess.TimeoutExpired) as exc:
                if isinstance(exc, RunnerError) and "output exceeded" in str(exc):
                    raise
                attempts += 1
                if retry_mode == "read":
                    allowed = self.args.read_retries == 0 or attempts <= self.args.read_retries
                else:
                    allowed = attempts <= (self.args.read_retries or 3)
                if not allowed:
                    raise RunnerError("safe command retries exhausted") from exc
                self.retry_count += 1
                self.health("reconnecting", failure_category="transport")
                delay = min(15, self.args.retry_base_seconds * 2 ** min(attempts - 1, 4))
                if self.in_flight_send:
                    time.sleep(delay)
                else:
                    reason = self.delay(delay)
                    if reason:
                        raise ControlledStop(reason)


    def cli(self, base, *parts, data=None, mode="read"):
        if mode == "send":
            return command(base + list(parts), data=data)
        return self.safe_command(base, *parts, data=data, retry_mode=mode)

    def cli_json(self, base, *parts, data=None, mode="read"):
        return one_json(self.cli(base, *parts, data=data, mode=mode))

    def outbox_list(self):
        try:
            items = json.loads(self.cli(self.outbox, "list"))
        except ValueError as exc:
            raise RunnerError("invalid outbox list") from exc
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise RunnerError("invalid outbox list")
        return items

    def initialize(self):
        status = self.cli_json(self.channel, "status")
        binding = status["binding"]
        keys = ("channel_id", "session_id", "account", "talker", "source", "encrypted_snapshot")
        identity = {key: binding.get(key) for key in keys}
        if not binding.get("talker", "").endswith("@chatroom"):
            raise RunnerError("channel must be bound to one group")
        config = {"binding": identity, "consumer": self.args.consumer,
                  "agent_command": self.args.agent_command, "sender_driver": str(self.args.sender_driver)}
        if self.state_path.exists() or self.state_path.is_symlink():
            state = load(self.state_path)
            if any(state.get(key) != value for key, value in config.items()):
                raise RunnerError("runner binding or command drift; refusing restart")
            if self.args.start_after is not None and self.args.start_after != state["start_after"]:
                raise RunnerError("startup cursor drift")
        else:
            baseline = self.args.start_after
            if baseline is None or baseline < 0 or baseline > status["latest_cursor"]:
                raise RunnerError("first run requires --start-after within current journal")
            state = {"version": 1, **config, "start_after": baseline, "pending": None}
            save(self.state_path, state)
        self.state = state
        self.last_cursor = state.get("last_cursor", state["start_after"])
        # This also recovers a crash between writing the baseline and acking it.
        if not state.get("started"):
            self.cli_json(self.channel, "ack", "--consumer", self.args.consumer,
                          "--cursor", str(state["start_after"]), mode="idempotent")
            state["started"] = True
            save(self.state_path, state)
        self.health("reading")
        return state

    def context(self, cursor):
        raw = self.cli(self.channel, "events", "--after", str(max(0, cursor - 13)), "--limit", "13")
        context = []
        for line in raw.splitlines():
            event = one_json(line)
            if event.get("cursor", cursor) >= cursor:
                continue
            body = event.get("message", {}).get("content", {})
            text = body.get("Text") if isinstance(body, dict) else None
            if isinstance(text, str):
                context.append({"cursor": event["cursor"], "sender": event["message"].get("sender"),
                                "direction": event.get("direction"), "text": text[:1000]})
        return context[-12:]

    def ack(self, cursor):
        result = self.cli_json(self.channel, "ack", "--consumer", self.args.consumer,
                               "--cursor", str(cursor), mode="idempotent")
        if result.get("cursor") != cursor:
            raise RunnerError("ack cursor mismatch")
        self.last_cursor = max(self.last_cursor, cursor)
        self.state["last_cursor"] = self.last_cursor
        save(self.state_path, self.state)
        self.health("reading")

    def process_pending(self, state):
        pending = state["pending"]
        if pending is None:
            return
        request_id = pending["request_id"]
        if pending["phase"] == "prepared":
            # Queue is idempotent for the exact same request ID and body.
            result = self.cli_json(self.outbox, "queue", "--request-id", request_id,
                                   "--channel-id", state["binding"]["channel_id"],
                                   "--session-id", state["binding"]["session_id"],
                                   "--text-stdin", data=pending["text"], mode="idempotent")
            if result.get("status") not in ("queued", "attempt_recorded", "ui_observed", "recipient_confirmed", "failed", "indeterminate"):
                raise RunnerError("unexpected queue result")
        item = self.cli_json(self.outbox, "show", "--request-id", request_id)
        if item.get("request_id") != request_id or item.get("text") != pending["text"]:
            raise RunnerError("outbox request content mismatch")
        status = item.get("status")
        if status == "queued":
            reason = self.boundary()
            if reason:
                raise ControlledStop(reason)
            pending["phase"] = "queued"
            save(self.state_path, state)
            self.health("sending")
            self.in_flight_send = True
            try:
                self.cli_json(self.outbox, "send", "--request-id", request_id,
                              "--driver", str(self.args.sender_driver), mode="send")
            except (RunnerError, OSError, subprocess.TimeoutExpired):
                # The attempt may have reached the UI despite CLI failure.
                raise RunnerError("send failed or outcome unknown; inspect outbox, do not retry")
            item = self.cli_json(self.outbox, "show", "--request-id", request_id,
                                 mode="post_send")
            status = item.get("status")
        if status not in ("ui_observed", "recipient_confirmed"):
            raise RunnerError(f"outbox status {status!r}; stopped without UI replay")
        self.ack(pending["cursor"])
        self.in_flight_send = False
        state["pending"] = None
        save(self.state_path, state)

    def process_event(self, state, event):
        binding = state["binding"]
        for key in ("channel_id", "session_id", "account", "talker"):
            if event.get(key) != binding[key]:
                raise RunnerError("event binding mismatch")
        cursor = event.get("cursor")
        message = event.get("message")
        if not isinstance(cursor, int) or cursor <= 0 or not isinstance(message, dict):
            raise RunnerError("malformed event")
        if event.get("direction") not in ("incoming", "outgoing"):
            raise RunnerError("unknown message direction; stopped without ack")
        if event.get("kind") != "message_observed" or event.get("direction") == "outgoing":
            self.ack(cursor)
            return
        if message.get("talker") != binding["talker"] or message.get("server_id") != event.get("server_id"):
            raise RunnerError("message identity mismatch")
        body = message.get("content")
        if not isinstance(body, dict) or not isinstance(body.get("Text"), str):
            self.ack(cursor)
            return
        server_id = event["server_id"]
        if not isinstance(server_id, int) or server_id <= 0:
            raise RunnerError("stable positive server ID required")
        request_id = "reply-" + hashlib.sha256(f"{binding['channel_id']}:{server_id}".encode()).hexdigest()
        prior = [item for item in self.outbox_list() if item.get("request_id") == request_id]
        if prior:
            if len(prior) != 1 or prior[0].get("status") not in ("ui_observed", "recipient_confirmed"):
                raise RunnerError("prior reply is nonterminal; inspect outbox without replay")
            self.ack(cursor)
            return
        request_data = {"version": 1, "event": event,
                        "recent_context": self.context(cursor)}
        memory_path = self.args.state_dir / "agent-memory.json"
        if memory_path.exists() or memory_path.is_symlink():
            from wx_agent.memory import MemoryError, load_memory
            try:
                memory = load_memory(self.args.state_dir, self.args.wx_cli)
            except MemoryError as exc:
                raise RunnerError("session memory binding or state invalid") from exc
            if any(memory["binding"].get(key) != binding[key]
                   for key in ("channel_id", "session_id", "account", "talker")):
                raise RunnerError("session memory binding mismatch")
            request_data["session_memory"] = memory
        request = json.dumps(request_data, ensure_ascii=False)
        response = None
        self.health("generating")
        for attempt in range(3):
            reason = self.boundary()
            if reason:
                raise ControlledStop(reason)
            try:
                response = one_json(command(self.args.agent_command, data=request,
                                            timeout=45, limit=MAX_REPLY + 1024))
                break
            except (RunnerError, OSError, subprocess.TimeoutExpired) as exc:
                if attempt == 2:
                    raise RunnerError("agent failed after 3 attempts") from exc
                self.retry_count += 1
                self.health("generating", failure_category="model")
                reason = self.delay(min(15, self.args.retry_base_seconds * 2 ** attempt))
                if reason:
                    raise ControlledStop(reason)
        self.health("generating")
        if response == {"skip": True}:
            self.ack(cursor)
            return
        text = response.get("text")
        if set(response) != {"text"} or not isinstance(text, str) or not text.strip() or "\x00" in text or len(text.encode()) > MAX_REPLY:
            raise RunnerError("agent must return nonempty bounded text or skip")
        state["pending"] = {"cursor": cursor, "server_id": server_id,
                            "request_id": request_id, "text": text, "phase": "prepared"}
        save(self.state_path, state)
        self.process_pending(state)

    def run(self):
        self.deadline = time.monotonic() + self.args.max_seconds if self.args.max_seconds else None
        self.health("reading")
        try:
            state = self.initialize()
            self.process_pending(state)
            processed = 0
            while True:
                reason = self.boundary()
                if reason:
                    raise ControlledStop(reason)
                self.health("reading")
                raw = self.cli(self.channel, "subscribe", "--consumer", self.args.consumer, "--once")
                events = [one_json(line) for line in raw.splitlines() if line.strip()]
                for event in events:
                    reason = self.boundary()
                    if reason:
                        raise ControlledStop(reason)
                    self.process_event(state, event)
                    processed += 1
                    if self.args.max_messages and processed >= self.args.max_messages:
                        raise ControlledStop("max_messages")
                if self.args.once:
                    raise ControlledStop("once")
                reason = self.delay(self.args.poll_seconds)
                if reason:
                    raise ControlledStop(reason)
        except ControlledStop as stop:
            self.health("stopped", terminal_reason=str(stop), exit_code=0)
        except (RunnerError, OSError, ValueError, UnicodeError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            message = str(exc)
            if "binding" in message or "identity" in message:
                category = "binding"
            elif "direction" in message:
                category = "unknown_direction"
            elif "JSON" in message or "malformed" in message or isinstance(exc, UnicodeError):
                category = "invalid_json"
            elif "control" in message:
                category = "control"
            elif self.stage == "generating":
                category = "model"
            elif self.stage == "sending":
                category = "send"
            elif self.stage == "reconnecting":
                category = "transport"
            else:
                category = "state"
            self.health("error", failure_category=category,
                        terminal_reason="error", exit_code=1)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wx-cli", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--agent-command-json", required=True)
    parser.add_argument("--sender-driver", type=Path, required=True)
    parser.add_argument("--start-after", type=int)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=3)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--max-seconds", type=float,
                        help="stop before starting another event or poll after this time; an in-flight command has its own timeout")
    parser.add_argument("--read-retries", type=int, default=3,
                        help="transient safe-read retries; 0 means unlimited with bounded backoff")
    parser.add_argument("--retry-base-seconds", type=float, default=1.0)
    args = parser.parse_args()
    try:
        args.agent_command = json.loads(args.agent_command_json)
        if not isinstance(args.agent_command, list) or not args.agent_command or not all(isinstance(p, str) for p in args.agent_command):
            raise RunnerError("agent command must be a nonempty JSON array of strings")
        if not args.state_dir.is_absolute() or not args.state_dir.exists():
            raise RunnerError("absolute existing state directory required")
        private(args.state_dir, True)
        executable(args.wx_cli)
        if not args.sender_driver.is_absolute():
            raise RunnerError("sender driver requires an absolute path")
        executable(Path(args.agent_command[0]))
        if (not args.consumer.strip() or args.poll_seconds < 1 or args.read_retries < 0
                or args.retry_base_seconds <= 0 or
                (args.max_messages is not None and args.max_messages < 1) or
                (args.max_seconds is not None and args.max_seconds <= 0)):
            raise RunnerError("invalid consumer, retry, or duration option")
        lock_path = args.state_dir / LOCK_NAME
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            private(lock_path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunnerError("runner lock held") from exc
            Runner(args).run()
        finally:
            os.close(fd)
    except (RunnerError, OSError, ValueError, UnicodeError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"channel agent stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
