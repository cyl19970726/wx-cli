"""Manual conversation on the one channel already bound to a controller config."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from . import channel_agent as runner
from . import wechat_agent_ctl as ctl


SELECTION_NAME = "channel-agent-chat-selection.json"
LEASE_NAME = ctl.INTERACTIVE_NAME
BINDING_KEYS = ("channel_id", "session_id", "account", "talker")
STATE_KEYS = ("channel_id", "session_id", "account", "talker", "source", "encrypted_snapshot")
TERMINAL = ("ui_observed", "recipient_confirmed")


class ChatError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _check(condition, code, message):
    if not condition:
        raise ChatError(code, message)


def _fingerprint(settings):
    raw = json.dumps(settings, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@contextmanager
def _lock(state_dir, name, blocking=True):
    path = state_dir / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        ctl.private(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ChatError("lock_busy", name + " is busy") from exc
        yield
    finally:
        os.close(fd)


def _read_optional(path):
    if not path.exists() and not path.is_symlink():
        return None
    return ctl.read_json(path)


def _command(settings, section, *parts, data=None):
    argv = [settings["wx_cli"], section, "--state-dir", settings["state_dir"], *parts]
    try:
        return runner.command(argv, data=data, timeout=75)
    except (runner.RunnerError, OSError, subprocess.SubprocessError) as exc:
        raise ChatError("command_failed", "local " + section + " command failed") from exc


def _json(settings, section, *parts, data=None):
    try:
        return runner.one_json(_command(settings, section, *parts, data=data))
    except runner.RunnerError as exc:
        raise ChatError("invalid_output", "local command returned invalid JSON") from exc


def _lines(settings, section, *parts):
    raw = _command(settings, section, *parts)
    try:
        values = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise ChatError("invalid_output", "local command returned invalid NDJSON") from exc
    _check(all(isinstance(value, dict) for value in values), "invalid_output", "event object required")
    return values


def _status(settings):
    result = _json(settings, "channel", "status", "--consumer", settings["consumer"])
    binding = result.get("binding")
    _check(isinstance(binding, dict) and all(isinstance(binding.get(key), str) and binding[key] for key in BINDING_KEYS),
           "binding", "channel binding is invalid")
    for key in ("consumer_cursor", "latest_cursor"):
        _check(type(result.get(key)) is int and result[key] >= 0, "cursor", "channel cursor is invalid")
    _check(result["consumer_cursor"] <= result["latest_cursor"], "cursor", "consumer cursor is ahead of journal")
    return result


def _selection(settings, binding):
    selected = _read_optional(Path(settings["state_dir"]) / SELECTION_NAME)
    _check(selected is not None, "selection_required", "select the bound group first")
    _check(selected.get("version") == 1 and selected.get("config_fingerprint") == _fingerprint(settings)
           and selected.get("binding") == {key: binding.get(key) for key in STATE_KEYS}
           and selected.get("talker") == binding["talker"], "selection_drift", "group selection or config changed")
    return selected


def _lease(settings, binding, active=True):
    lease = _read_optional(Path(settings["state_dir"]) / LEASE_NAME)
    _check(lease is not None, "takeover_required", "take over the runner first")
    _check(lease.get("version") == 1 and lease.get("config_fingerprint") == _fingerprint(settings)
           and lease.get("binding") == {key: binding.get(key) for key in STATE_KEYS},
           "lease_drift", "interactive lease binding or config changed")
    _check(lease.get("phase") in ("active", "handback"), "lease_invalid", "invalid lease phase")
    if active:
        _check(lease["phase"] == "active", "handback_in_progress", "handback is already in progress")
    return lease


def _runner_state(settings, binding):
    path = Path(settings["state_dir"]) / runner.STATE_NAME
    state = runner.load(path)
    expected = {key: binding.get(key) for key in STATE_KEYS}
    _check(state.get("binding") == expected and state.get("consumer") == settings["consumer"]
           and state.get("agent_command") == settings["agent_command"]
           and state.get("sender_driver") == settings["sender_driver"] and state.get("started") is True,
           "runner_drift", "runner binding or command differs from controller")
    cursor = state.get("last_cursor", state.get("start_after"))
    _check(type(cursor) is int and cursor >= 0, "runner_state", "runner cursor is invalid")
    return path, state, cursor


def _outbox_list(settings):
    try:
        items = json.loads(_command(settings, "outbox", "list"))
    except (ValueError, UnicodeError) as exc:
        raise ChatError("invalid_output", "outbox list is invalid") from exc
    _check(isinstance(items, list) and all(isinstance(item, dict) for item in items),
           "invalid_output", "outbox list is invalid")
    return items


def _outbox_item(settings, request_id):
    return next((item for item in _outbox_list(settings) if item.get("request_id") == request_id), None)


def _outbox_risk(settings, binding, consumer_cursor):
    items = _outbox_list(settings)
    uncertain = {item.get("request_id") for item in items
                 if item.get("status") in ("failed", "indeterminate")}
    blocked = [item for item in items if item.get("status") in ("queued", "attempt_recorded")]
    cursor = consumer_cursor
    while uncertain:
        page = _events(settings, cursor, 1000, binding)
        if not page:
            break
        for event in page:
            if event.get("kind") == "message_observed" and event["direction"] == "incoming":
                request_id = _request_id(binding, event["server_id"])
                if request_id in uncertain:
                    blocked.append({"request_id": request_id, "status": "unresolved_source"})
        cursor = page[-1]["cursor"]
    return {"blocked": blocked, "historical_uncertain_count": len(uncertain)}


def _check_outbox_clear(settings, binding, consumer_cursor):
    risk = _outbox_risk(settings, binding, consumer_cursor)
    _check(not risk["blocked"], "outbox_pending", "outbox has an unresolved request for this handoff")
    return risk


def _check_event(event, binding):
    _check(all(event.get(key) == binding[key] for key in BINDING_KEYS), "binding", "event binding differs")
    _check(type(event.get("cursor")) is int and event["cursor"] > 0 and isinstance(event.get("message"), dict),
           "event", "event identity is invalid")
    _check(event.get("direction") in ("incoming", "outgoing"), "unknown_direction", "event direction is unknown")
    _check(event["message"].get("talker") == binding["talker"]
           and event["message"].get("server_id") == event.get("server_id"), "event", "message identity differs")


def _events(settings, after, limit, binding):
    result = _lines(settings, "channel", "events", "--after", str(after), "--limit", str(limit))
    last = after
    for event in result:
        _check_event(event, binding)
        _check(event["cursor"] > last, "event", "events are not strictly ordered")
        last = event["cursor"]
    return result


def _request_id(binding, server_id):
    _check(type(server_id) is int and server_id > 0, "event", "stable server ID required")
    return runner.reply_request_id(binding["channel_id"], server_id)


def _text_match(item, text):
    if item.get("text") != text:
        raise ChatError("text_conflict", "existing request has different text",
                        {"request_id": item.get("request_id"), "status": item.get("status")})


def _target_event(settings, binding, after, target):
    _check(type(target) is int and target > after, "cursor_order", "reply cursor is already processed")
    cursor = after
    while cursor < target:
        page = _events(settings, cursor, 1000, binding)
        _check(page, "cursor_missing", "reply cursor is absent from local journal")
        for event in page:
            current = event["cursor"]
            _check(current <= target, "cursor_missing", "reply cursor is absent from local journal")
            body = event["message"].get("content")
            is_text = (event.get("kind") == "message_observed" and event["direction"] == "incoming"
                       and isinstance(body, dict) and isinstance(body.get("Text"), str))
            if current < target:
                _check(not is_text, "earlier_incoming", "an earlier incoming text must be handled first")
                cursor = current
                continue
            _check(is_text, "not_replyable", "reply cursor is not an incoming observed text")
            return event
        cursor = page[-1]["cursor"]
    raise ChatError("cursor_missing", "reply cursor is absent from local journal")


def _ack(settings, previous, cursor):
    result = _json(settings, "channel", "ack", "--consumer", settings["consumer"],
                   "--cursor", str(cursor), "--expected-cursor", str(previous))
    _check(result.get("cursor") == cursor, "cursor", "ack cursor differs")


def _finish_pending(settings, binding, lease, item):
    pending = lease["pending"]
    _check(item.get("request_id") == pending["request_id"], "outbox_drift", "outbox request ID differs")
    _text_match(item, pending["text"])
    _check(item.get("status") in TERMINAL, "send_uncertain", "send outcome is not terminal")
    status = _status(settings)
    current = status["consumer_cursor"]
    _check(current in (pending["previous_cursor"], pending["cursor"]),
           "cursor_drift", "consumer cursor changed during manual reply")
    if current == pending["previous_cursor"]:
        _ack(settings, current, pending["cursor"])
    state_path, state, state_cursor = _runner_state(settings, binding)
    _check(state.get("pending") is None and state_cursor in (pending["previous_cursor"], pending["cursor"]),
           "runner_drift", "runner state changed during manual reply")
    state["last_cursor"] = pending["cursor"]
    runner.save(state_path, state)
    lease["pending"] = None
    runner.save(Path(settings["state_dir"]) / LEASE_NAME, lease)
    return {"request_id": pending["request_id"], "cursor": pending["cursor"],
            "status": item["status"], "already_completed": current == pending["cursor"]}


def _skip_record(state, cursor):
    records = state.get("manual_skips", [])
    _check(isinstance(records, list) and all(isinstance(item, dict)
           and type(item.get("cursor")) is int and isinstance(item.get("reason"), str)
           and type(item.get("server_id")) is int and item["server_id"] > 0
           for item in records), "runner_state", "manual skip audit is invalid")
    matching = [item for item in records if item["cursor"] == cursor]
    _check(len(matching) <= 1, "runner_state", "manual skip audit has duplicate cursor")
    return matching[0] if matching else None


def _completed_skip(record, reason):
    _check(record["reason"] == reason, "skip_conflict", "cursor was skipped with a different reason")
    return {"cursor": record["cursor"], "reason": reason, "status": "skipped", "already_completed": True}


def groups(settings, name, exact=False):
    binding = _status(settings)["binding"]
    _check(isinstance(binding.get("source"), str) and binding["source"], "binding", "channel source is missing")
    argv = [settings["wx_cli"], "groups", "find", "--state-dir", settings["state_dir"],
            "--account", binding["account"], "--source", binding["source"], "--name", name]
    if exact:
        argv.append("--exact")
    try:
        result = runner.one_json(runner.command(argv, timeout=75))
    except (runner.RunnerError, OSError, subprocess.SubprocessError) as exc:
        raise ChatError("command_failed", "local group lookup failed") from exc
    _check(result.get("match_kind") in ("exact", "partial", "none") and isinstance(result.get("matches"), list),
           "invalid_output", "group finder returned invalid results")
    _check(all(isinstance(item, dict) and isinstance(item.get("talker"), str)
               and isinstance(item.get("name"), str) for item in result["matches"]),
           "invalid_output", "group finder returned invalid match")
    return result


def select(settings, talker):
    state_dir = Path(settings["state_dir"])
    with _lock(state_dir, ctl.LOCK_NAME):
        binding = _status(settings)["binding"]
        _check(talker == binding["talker"], "different_group", "only the configured group can be selected")
        selected = {"version": 1, "config_fingerprint": _fingerprint(settings),
                    "binding": {key: binding.get(key) for key in STATE_KEYS}, "talker": talker}
        existing = _read_optional(state_dir / SELECTION_NAME)
        _check(existing is None or existing == selected, "selection_drift", "existing group selection differs")
        runner.save(state_dir / SELECTION_NAME, selected)
        return {"selected": True, "binding": selected["binding"]}


def read(settings, after=None, limit=50):
    binding = _status(settings)["binding"]
    _selection(settings, binding)
    _check(type(limit) is int and 1 <= limit <= 999, "limit", "limit must be 1..999")
    _json(settings, "channel", "sync")
    status = _status(settings)
    _check(status["binding"] == binding, "binding", "channel binding changed during sync")
    cursor = status["consumer_cursor"] if after is None else after
    _check(type(cursor) is int and cursor >= 0, "cursor", "after must be a nonnegative cursor")
    events = _events(settings, cursor, limit + 1, binding)
    visible = events[:limit]
    return {"binding": {key: binding.get(key) for key in STATE_KEYS}, "consumer_cursor": status["consumer_cursor"],
            "after": cursor, "next_cursor": visible[-1]["cursor"] if visible else cursor,
            "events": visible, "has_more": len(events) > limit,
            "latest_cursor": status["latest_cursor"], "history_complete": False}


def takeover(settings, wait_seconds=15):
    state_dir = Path(settings["state_dir"])
    with _lock(state_dir, ctl.LOCK_NAME):
        status = _status(settings)
        binding = status["binding"]
        _selection(settings, binding)
        _check(_read_optional(state_dir / LEASE_NAME) is None, "lease_exists", "interactive lease already exists")
        running = ctl.status(settings)["alive"]
        if running:
            ctl.write_control(state_dir, "stop")
            end = time.monotonic() + wait_seconds
            while ctl.status(settings)["alive"] and time.monotonic() < end:
                time.sleep(0.1)
            _check(not ctl.status(settings)["alive"], "stop_incomplete", "runner has not exited")
        with _lock(state_dir, runner.LOCK_NAME, blocking=False):
            _check(_read_optional(state_dir / LEASE_NAME) is None, "lease_exists", "interactive lease already exists")
            _json(settings, "channel", "sync")
            status = _status(settings)
            _check(status["binding"] == binding, "binding", "channel binding changed")
            _, state, state_cursor = _runner_state(settings, binding)
            _check(state.get("pending") is None, "runner_pending", "runner has a pending reply")
            risk = _check_outbox_clear(settings, binding, status["consumer_cursor"])
            _check(state_cursor == status["consumer_cursor"], "cursor_drift", "runner and consumer cursors differ")
            lease = {"version": 1, "phase": "active", "config_fingerprint": _fingerprint(settings),
                     "binding": {key: binding.get(key) for key in STATE_KEYS},
                     "previously_alive": running, "pending": None, "consumer_cursor": state_cursor}
            runner.save(state_dir / LEASE_NAME, lease)
            return {"taken_over": True, "previously_alive": running, "consumer_cursor": state_cursor,
                    "binding": lease["binding"], "historical_uncertain_count": risk["historical_uncertain_count"]}


def reply(settings, cursor, text):
    _check(isinstance(text, str) and text.strip() and "\x00" not in text
           and len(text.encode("utf-8")) <= runner.MAX_REPLY, "text", "reply text must be nonempty and at most 64 KiB")
    state_dir = Path(settings["state_dir"])
    with _lock(state_dir, ctl.LOCK_NAME), _lock(state_dir, runner.LOCK_NAME, blocking=False):
        status = _status(settings)
        binding = status["binding"]
        _selection(settings, binding)
        lease = _lease(settings, binding)
        _check(not ctl.status(settings)["alive"], "runner_active", "automatic runner is still active")
        _, state, state_cursor = _runner_state(settings, binding)
        _check(state.get("pending") is None, "runner_pending", "runner has a pending reply")
        pending = lease.get("pending")
        if pending is not None:
            _check(pending.get("kind") in (None, "reply"), "pending_conflict", "a manual skip is pending")
            _check(pending.get("cursor") == cursor and pending.get("text") == text,
                   "pending_conflict", "another manual reply is pending")
            _check(state_cursor in (pending.get("previous_cursor"), cursor)
                   and status["consumer_cursor"] in (pending.get("previous_cursor"), cursor),
                   "cursor_drift", "manual reply cursor changed")
            _check(not (state_cursor == cursor and status["consumer_cursor"] != cursor),
                   "cursor_drift", "runner state is ahead of consumer cursor")
            request_id = pending["request_id"]
        else:
            _check(state_cursor == status["consumer_cursor"], "cursor_drift", "runner and consumer cursors differ")
            _json(settings, "channel", "sync")
            status = _status(settings)
            _check(status["binding"] == binding and status["consumer_cursor"] == state_cursor,
                   "cursor_drift", "channel changed before reply")
            if cursor <= state_cursor:
                # An already-acked source is never sent again. Only the exact
                # existing intent may be reported as completed.
                events = _events(settings, cursor - 1, 1, binding)
                _check(events and events[0]["cursor"] == cursor, "cursor_missing", "reply source is absent")
                request_id = _request_id(binding, events[0].get("server_id"))
                existing = _outbox_item(settings, request_id)
                _check(existing is not None, "cursor_order", "reply cursor is already processed")
                item = _json(settings, "outbox", "show", "--request-id", request_id)
                _text_match(item, text)
                _check(item.get("status") in TERMINAL, "send_uncertain", "existing send is unresolved")
                return {"request_id": request_id, "cursor": cursor, "status": item["status"],
                        "already_completed": True}
            event = _target_event(settings, binding, state_cursor, cursor)
            request_id = _request_id(binding, event["server_id"])
            existing = _outbox_item(settings, request_id)
            if existing is not None:
                item = _json(settings, "outbox", "show", "--request-id", request_id)
                _text_match(item, text)
                _check(item.get("status") in TERMINAL, "send_uncertain", "existing send is unresolved")
                lease["pending"] = {"cursor": cursor, "previous_cursor": state_cursor,
                                    "request_id": request_id, "text": text, "phase": "terminal"}
                runner.save(state_dir / LEASE_NAME, lease)
                return _finish_pending(settings, binding, lease, item)
            lease["pending"] = {"cursor": cursor, "previous_cursor": state_cursor,
                                "request_id": request_id, "text": text, "phase": "prepared"}
            runner.save(state_dir / LEASE_NAME, lease)
        existing = _outbox_item(settings, request_id)
        if pending is not None and status["consumer_cursor"] == cursor:
            _check(existing is not None, "cursor_drift", "ack advanced without a durable outbox request")
        if existing is None:
            _json(settings, "outbox", "queue", "--request-id", request_id,
                  "--channel-id", binding["channel_id"], "--session-id", binding["session_id"],
                  "--text-stdin", data=text)
        item = _json(settings, "outbox", "show", "--request-id", request_id)
        _check(item.get("request_id") == request_id, "outbox_drift", "outbox request ID differs")
        _text_match(item, text)
        if item.get("status") == "queued":
            lease["pending"]["phase"] = "queued"
            runner.save(state_dir / LEASE_NAME, lease)
            try:
                _json(settings, "outbox", "send", "--request-id", request_id,
                      "--driver", settings["sender_driver"])
            except ChatError:
                # The attempt may already have reached the GUI. Show state only;
                # never issue another send or switch request IDs.
                pass
            item = _json(settings, "outbox", "show", "--request-id", request_id)
        _check(item.get("status") in TERMINAL, "send_uncertain", "send result requires manual inspection")
        return _finish_pending(settings, binding, lease, item)


def recover_unsent_reply(settings, cursor, expected_attempt=None):
    """Explicitly recover the same pending reply after the operator verifies it was unsent."""
    _check(type(cursor) is int and cursor > 0, "cursor", "positive cursor required")
    state_dir = Path(settings["state_dir"])
    with _lock(state_dir, ctl.LOCK_NAME), _lock(state_dir, runner.LOCK_NAME, blocking=False):
        status = _status(settings)
        binding = status["binding"]
        _selection(settings, binding)
        lease = _lease(settings, binding)
        _check(not ctl.status(settings)["alive"], "runner_active", "automatic runner is still active")
        _, state, state_cursor = _runner_state(settings, binding)
        _check(state.get("pending") is None, "runner_pending", "runner has a pending reply")
        pending = lease.get("pending")
        _check(isinstance(pending, dict) and pending.get("kind") in (None, "reply")
               and pending.get("cursor") == cursor,
               "pending_conflict", "no retained reply matches this cursor")
        _check(isinstance(pending.get("request_id"), str) and isinstance(pending.get("text"), str),
               "lease_invalid", "retained reply identity is invalid")
        previous = pending.get("previous_cursor")
        _check(type(previous) is int and previous >= 0, "lease_invalid", "retained reply cursor is invalid")
        _check(state_cursor in (pending.get("previous_cursor"), cursor)
               and status["consumer_cursor"] in (pending.get("previous_cursor"), cursor),
               "cursor_drift", "manual reply cursor changed")
        source = _target_event(settings, binding, previous, cursor)
        _check(pending["request_id"] == _request_id(binding, source.get("server_id")),
               "outbox_drift", "retained reply request does not match its source cursor")
        item = _json(settings, "outbox", "show", "--request-id", pending["request_id"])
        _check(item.get("request_id") == pending["request_id"], "outbox_drift", "outbox request ID differs")
        _text_match(item, pending["text"])
        _check(item.get("status") in ("failed", "indeterminate"),
               "send_uncertain", "only a failed or indeterminate reply can be recovered")
        attempt = item.get("attempt_number")
        _check(type(attempt) is int and attempt > 0, "outbox_drift", "outbox attempt number is invalid")
        _check(expected_attempt is None or type(expected_attempt) is int and attempt == expected_attempt,
               "attempt_drift", "outbox attempt differs from the requested recovery")
        _json(settings, "outbox", "recover-unsent", "--request-id", pending["request_id"],
              "--expected-attempt", str(attempt), "--driver", settings["sender_driver"])
        recovered = _json(settings, "outbox", "show", "--request-id", pending["request_id"])
        return _finish_pending(settings, binding, lease, recovered)


def skip(settings, cursor, reason):
    _check(isinstance(reason, str) and reason.strip() and "\x00" not in reason
           and len(reason.encode("utf-8")) <= 1000,
           "reason", "skip reason must be nonempty and at most 1000 bytes")
    state_dir = Path(settings["state_dir"])
    with _lock(state_dir, ctl.LOCK_NAME), _lock(state_dir, runner.LOCK_NAME, blocking=False):
        status = _status(settings)
        binding = status["binding"]
        _selection(settings, binding)
        lease = _lease(settings, binding)
        _check(not ctl.status(settings)["alive"], "runner_active", "automatic runner is still active")
        state_path, state, state_cursor = _runner_state(settings, binding)
        _check(state.get("pending") is None, "runner_pending", "runner has a pending reply")
        pending = lease.get("pending")
        if pending is not None:
            _check(pending.get("kind") == "skip", "pending_conflict", "a manual reply is pending")
            _check(pending.get("cursor") == cursor and pending.get("reason") == reason,
                   "pending_conflict", "another manual skip is pending")
            previous = pending.get("previous_cursor")
            _check(type(previous) is int and previous >= 0
                   and state_cursor in (previous, cursor)
                   and status["consumer_cursor"] in (previous, cursor)
                   and not (state_cursor == cursor and status["consumer_cursor"] != cursor),
                   "cursor_drift", "manual skip cursor changed")
            event = _target_event(settings, binding, previous, cursor)
            _check(event["server_id"] == pending.get("server_id"),
                   "event", "manual skip source changed")
        else:
            _check(state_cursor == status["consumer_cursor"],
                   "cursor_drift", "runner and consumer cursors differ")
            if cursor <= state_cursor:
                record = _skip_record(state, cursor)
                _check(record is not None, "cursor_order", "cursor is already processed")
                _check(_outbox_item(settings, _request_id(binding, record["server_id"])) is None,
                       "existing_reply", "source already has an outbox request")
                return _completed_skip(record, reason)
            _json(settings, "channel", "sync")
            status = _status(settings)
            _check(status["binding"] == binding and status["consumer_cursor"] == state_cursor,
                   "cursor_drift", "channel changed before skip")
            event = _target_event(settings, binding, state_cursor, cursor)
            previous = state_cursor
            request_id = _request_id(binding, event["server_id"])
            _check(_outbox_item(settings, request_id) is None,
                   "existing_reply", "source already has an outbox request")
            lease["pending"] = {"kind": "skip", "cursor": cursor,
                                "previous_cursor": previous, "server_id": event["server_id"],
                                "reason": reason}
            runner.save(state_dir / LEASE_NAME, lease)
        request_id = _request_id(binding, event["server_id"])
        _check(_outbox_item(settings, request_id) is None,
               "existing_reply", "source already has an outbox request")
        current = status["consumer_cursor"]
        if current == previous:
            _ack(settings, previous, cursor)
        record = _skip_record(state, cursor)
        if record is None:
            _check(state_cursor == previous, "runner_drift", "skip audit is missing")
            state.setdefault("manual_skips", []).append({"cursor": cursor,
                                                          "server_id": event["server_id"], "reason": reason})
            state["last_cursor"] = cursor
            runner.save(state_path, state)
        else:
            _completed_skip(record, reason)
            _check(state_cursor == cursor, "runner_drift", "skip audit cursor differs")
        lease["pending"] = None
        runner.save(state_dir / LEASE_NAME, lease)
        return {"cursor": cursor, "reason": reason, "status": "skipped",
                "already_completed": current == cursor}


def handback(settings):
    state_dir = Path(settings["state_dir"])
    lease_path = state_dir / LEASE_NAME
    ready_path = state_dir / runner.HANDOFF_READY_NAME
    with _lock(state_dir, ctl.LOCK_NAME):
        status = _status(settings)
        binding = status["binding"]
        _selection(settings, binding)
        lease = _lease(settings, binding, active=False)
        pending = lease.get("pending")
        _check(pending is None, "manual_pending",
               "manual skip is pending" if isinstance(pending, dict) and pending.get("kind") == "skip"
               else "manual reply is pending")
        _check(type(lease.get("previously_alive")) is bool, "lease_invalid", "original runner state is missing")
        if lease["phase"] == "active":
            with _lock(state_dir, runner.LOCK_NAME, blocking=False):
                _check(not ctl.status(settings)["alive"], "runner_active", "automatic runner is active")
                _, state, state_cursor = _runner_state(settings, binding)
                _check(state.get("pending") is None, "runner_pending", "runner has a pending reply")
                _check(state_cursor == status["consumer_cursor"], "cursor_drift", "runner and consumer cursors differ")
                _check_outbox_clear(settings, binding, status["consumer_cursor"])
                if not lease["previously_alive"]:
                    lease_path.unlink()
                    return {"handed_back": True, "recovered": False, "alive": False}
                lease["phase"] = "handback"
                lease["handoff_token"] = secrets.token_hex(24)
                runner.save(lease_path, lease)
        _check(lease["previously_alive"], "lease_invalid", "stopped runner cannot enter startup handback")
        observed = ctl.status(settings)
        recovered = observed["alive"]
        if not recovered:
            if ready_path.exists() or ready_path.is_symlink():
                ctl.private(ready_path)
                ready_path.unlink()
            result = ctl.start(settings, handoff_token=lease["handoff_token"])
            pid = result["pid"]
        else:
            pid = observed["pid"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            observed = ctl.status(settings)
            if not observed["alive"] or observed["pid"] != pid:
                raise ChatError("handoff_child_exited", "handback runner exited before ready")
            ready = _read_optional(ready_path)
            if ready is not None:
                _check(ready.get("version") == 1 and ready.get("pid") == pid
                       and ready.get("handoff_token") == lease["handoff_token"],
                       "handoff_ready_mismatch", "handback ready identity differs")
                break
            time.sleep(0.1)
        else:
            raise ChatError("handoff_not_ready", "handback runner did not become ready")
        final_status = ctl.status(settings)
        _check(final_status["alive"] and final_status["pid"] == pid,
               "handoff_child_exited", "handback runner exited before release")
        lease_path.unlink()
        ready_path.unlink()
        return {"handed_back": True, "recovered": recovered, "alive": True, "pid": pid}


def status(settings):
    channel = _status(settings)
    state_dir = Path(settings["state_dir"])
    selected = _read_optional(state_dir / SELECTION_NAME)
    lease = _read_optional(state_dir / LEASE_NAME)
    if selected is not None:
        _selection(settings, channel["binding"])
    if lease is not None:
        _lease(settings, channel["binding"], active=False)
    risk = _outbox_risk(settings, channel["binding"], channel["consumer_cursor"])
    return {"binding": {key: channel["binding"].get(key) for key in STATE_KEYS},
            "consumer_cursor": channel["consumer_cursor"], "latest_cursor": channel["latest_cursor"],
            "selected": selected is not None, "interactive_phase": lease.get("phase") if lease else None,
            "manual_pending": bool(lease and lease.get("pending")), "runner": ctl.status(settings),
            "historical_uncertain_count": risk["historical_uncertain_count"],
            "outbox_blocked_count": len(risk["blocked"]),
            "history_complete": False}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wx-agent chat", description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="absolute private controller JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("groups")
    find.add_argument("--name", required=True)
    find.add_argument("--exact", action="store_true")
    choose = commands.add_parser("select")
    choose.add_argument("--talker", required=True)
    reading = commands.add_parser("read")
    reading.add_argument("--after", type=int)
    reading.add_argument("--limit", type=int, default=50)
    take = commands.add_parser("takeover")
    take.add_argument("--wait-seconds", type=float, default=15)
    send = commands.add_parser("reply")
    send.add_argument("--cursor", type=int, required=True)
    send.add_argument("--text-stdin", action="store_true", required=True)
    recover = commands.add_parser("recover-unsent-reply")
    recover.add_argument("--cursor", type=int, required=True)
    recover.add_argument("--expected-attempt", type=int, required=True)
    dismiss = commands.add_parser("skip")
    dismiss.add_argument("--cursor", type=int, required=True)
    dismiss.add_argument("--reason", required=True)
    commands.add_parser("handback")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        settings = ctl.config(args.config, require_executables=args.command in
                              ("takeover", "reply", "recover-unsent-reply", "handback"))
        if args.command == "groups":
            result = groups(settings, args.name, args.exact)
        elif args.command == "select":
            result = select(settings, args.talker)
        elif args.command == "read":
            result = read(settings, args.after, args.limit)
        elif args.command == "takeover":
            _check(0 <= args.wait_seconds <= 60, "wait", "wait-seconds must be 0..60")
            result = takeover(settings, args.wait_seconds)
        elif args.command == "reply":
            raw = sys.stdin.buffer.read(runner.MAX_REPLY + 1)
            _check(len(raw) <= runner.MAX_REPLY, "text", "reply text is too large")
            result = reply(settings, args.cursor, raw.decode("utf-8"))
        elif args.command == "recover-unsent-reply":
            result = recover_unsent_reply(settings, args.cursor, args.expected_attempt)
        elif args.command == "skip":
            result = skip(settings, args.cursor, args.reason)
        elif args.command == "handback":
            result = handback(settings)
        else:
            result = status(settings)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ChatError, ctl.ControlError, runner.RunnerError, OSError, ValueError, UnicodeError,
            subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, ChatError) else "local_state_error"
        details = exc.details if isinstance(exc, ChatError) else {}
        print(json.dumps({"error": {"code": code, "message": str(exc), **details}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
