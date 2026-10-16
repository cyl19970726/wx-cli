"""Private, explicitly curated notes for one bound wx-cli channel session.

The journal remains the source of truth. This file stores caller assertions with
verified journal cursor provenance; it never infers or confirms facts itself.
"""

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


MEMORY_NAME = "agent-memory.json"
LOCK_NAME = ".agent-memory.lock"
BINDING_KEYS = ("channel_id", "session_id", "account", "talker")
MAX_ITEMS = 32
MAX_REFS = 64
MAX_TEXT = 2000
MAX_SUMMARY = 6000
MAX_FILE = 100_000


class MemoryError(ValueError):
    """A private-state, provenance, or revision check failed."""


def _private(path, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise MemoryError("private state path is missing") from exc
    if (stat.S_ISLNK(info.st_mode) or
            not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600) or
            (not directory and info.st_nlink != 1)):
        raise MemoryError("state path must be private, owned, and unlinked")


def _state_path(state_dir):
    path = Path(state_dir)
    if not path.is_absolute():
        raise MemoryError("absolute state directory required")
    _private(path, directory=True)
    return path


def _command(wx_cli, state_dir, *args, timeout=5):
    executable = Path(wx_cli)
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise MemoryError("absolute wx-cli executable required")
    # Reuse the runner's bounded reader: subprocess.run(capture_output=True)
    # accumulates unlimited output before it can enforce a size limit.
    from .channel_agent import RunnerError, command
    try:
        return command([str(executable), "channel", "--state-dir", str(state_dir), *args],
                       timeout=timeout, limit=MAX_FILE)
    except (OSError, RunnerError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise MemoryError("channel read failed") from exc


def _json_object(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise MemoryError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise MemoryError("expected JSON object")
    return value


def _binding(state_dir, wx_cli):
    status = _json_object(_command(wx_cli, state_dir, "status"))
    binding = status.get("binding")
    if not isinstance(binding, dict) or any(not isinstance(binding.get(k), str) or not binding[k]
                                               for k in BINDING_KEYS):
        raise MemoryError("invalid channel binding")
    if not binding["talker"].endswith("@chatroom"):
        raise MemoryError("channel must be bound to one group")
    latest = status.get("latest_cursor")
    if type(latest) is not int or latest < 0:
        raise MemoryError("invalid channel cursor")
    return {key: binding[key] for key in BINDING_KEYS}, latest


def _empty(binding):
    return {"version": 1, "binding": binding, "revision": 0, "summary": "",
            "summary_source_cursors": [], "confirmed": [], "open_questions": []}


def _read(state_dir, binding, latest):
    path = state_dir / MEMORY_NAME
    try:
        _private(path)
    except MemoryError:
        if not path.exists() and not path.is_symlink():
            return _empty(binding)
        raise
    if path.stat().st_size > MAX_FILE:
        raise MemoryError("memory file too large")
    with path.open("r", encoding="utf-8") as stream:
        value = _json_object(stream.read(MAX_FILE + 1))
    if value.get("binding") != binding:
        raise MemoryError("memory binding differs from channel")
    if type(value.get("version")) is not int or value["version"] != 1 or type(value.get("revision")) is not int or value["revision"] < 1:
        raise MemoryError("invalid memory version or revision")
    if any(cursor > latest for cursor in _validate_content(value)):
        raise MemoryError("stored source cursor is in the future")
    return value


def _refs(value):
    if not isinstance(value, list) or len(value) > MAX_REFS or any(type(n) is not int or n <= 0 for n in value):
        raise MemoryError("invalid source cursors")
    if len(set(value)) != len(value):
        raise MemoryError("duplicate source cursor")
    return value


def _validate_content(value):
    allowed = {"version", "binding", "revision", "summary", "summary_source_cursors",
               "confirmed", "open_questions"}
    if set(value) - allowed:
        raise MemoryError("unexpected memory field")
    summary = value.get("summary")
    if not isinstance(summary, str) or len(summary) > MAX_SUMMARY:
        raise MemoryError("invalid summary")
    refs = _refs(value.get("summary_source_cursors"))
    if bool(summary.strip()) != bool(refs):
        raise MemoryError("summary and its source cursors are required together")
    all_refs = set(refs)
    for field in ("confirmed", "open_questions"):
        items = value.get(field)
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise MemoryError("invalid memory item list")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "source_cursors"}:
                raise MemoryError("invalid memory item")
            text = item["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise MemoryError("invalid memory item text")
            source_refs = _refs(item["source_cursors"])
            if not source_refs:
                raise MemoryError("memory item needs source cursors")
            all_refs.update(source_refs)
    if len(all_refs) > MAX_REFS:
        raise MemoryError("too many distinct source cursors")
    return all_refs


def _verify_sources(state_dir, wx_cli, binding, latest, refs):
    deadline = time.monotonic() + 15
    for cursor in sorted(refs):
        if cursor > latest:
            raise MemoryError("future source cursor")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MemoryError("source verification timed out")
        raw = _command(wx_cli, state_dir, "events", "--after", str(cursor - 1), "--limit", "1",
                       timeout=min(5, remaining))
        lines = raw.splitlines()
        if len(lines) != 1:
            raise MemoryError("source cursor is missing")
        event = _json_object(lines[0])
        if event.get("cursor") != cursor:
            raise MemoryError("source cursor is missing")
        if (event.get("kind") != "message_observed" or
                event.get("direction") != "incoming" or
                any(event.get(key) != binding[key] for key in BINDING_KEYS) or
                not isinstance(event.get("message"), dict) or
                event["message"].get("talker") != binding["talker"] or
                event["message"].get("server_id") != event.get("server_id")):
            raise MemoryError("source cursor is not an incoming event in this binding")


def load_memory(state_dir, wx_cli):
    """Read notes only when their incoming-event provenance still verifies."""
    state_dir = _state_path(state_dir)
    binding, latest = _binding(state_dir, wx_cli)
    stored = _read(state_dir, binding, latest)
    _verify_sources(state_dir, wx_cli, binding, latest, _validate_content(stored))
    return stored


def update_memory(state_dir, wx_cli, document, expected_revision):
    """Replace curated notes with CAS and verified incoming-event references."""
    state_dir = _state_path(state_dir)
    if type(expected_revision) is not int or expected_revision < 0:
        raise MemoryError("invalid expected revision")
    if not isinstance(document, dict):
        raise MemoryError("expected memory document")
    lock_path = state_dir / LOCK_NAME
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise MemoryError("unable to open private memory lock") from exc
    try:
        _private(lock_path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        binding, latest = _binding(state_dir, wx_cli)
        current = _read(state_dir, binding, latest)
        if current["revision"] != expected_revision:
            raise MemoryError("memory revision changed")
        if document.get("binding") != binding:
            raise MemoryError("update binding differs from channel")
        if type(document.get("version")) is not int or document["version"] != 1 or set(document) != {
                "version", "binding", "summary", "summary_source_cursors", "confirmed", "open_questions"}:
            raise MemoryError("invalid update schema")
        refs = _validate_content(document)
        _verify_sources(state_dir, wx_cli, binding, latest, refs)
        updated = {**document, "revision": expected_revision + 1}
        encoded = (json.dumps(updated, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_FILE:
            raise MemoryError("memory file too large")
        target = state_dir / MEMORY_NAME
        if target.exists() or target.is_symlink():
            _private(target)
        tmp = None
        try:
            fd_tmp, tmp = tempfile.mkstemp(prefix=".agent-memory-", dir=state_dir)
            os.fchmod(fd_tmp, 0o600)
            with os.fdopen(fd_tmp, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target)
            tmp = None
            dir_fd = os.open(state_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp is not None:
                os.unlink(tmp)
        return updated
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wx-agent memory")
    parser.add_argument("action", choices=("show", "update"))
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--wx-cli", required=True)
    parser.add_argument("--file")
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args(argv)
    try:
        if args.action == "show":
            if args.file is not None or args.expected_revision is not None:
                parser.error("show accepts no update arguments")
            result = load_memory(args.state_dir, args.wx_cli)
        else:
            if args.file is None or args.expected_revision is None:
                parser.error("update requires --file and --expected-revision")
            source = Path(args.file)
            _private(source)
            if source.stat().st_size > MAX_FILE:
                raise MemoryError("update file too large")
            with source.open("r", encoding="utf-8") as stream:
                document = _json_object(stream.read(MAX_FILE + 1))
            result = update_memory(args.state_dir, args.wx_cli, document, args.expected_revision)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (MemoryError, OSError, UnicodeError) as exc:
        print(f"wx-agent memory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
