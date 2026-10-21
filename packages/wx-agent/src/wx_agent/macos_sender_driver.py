#!/usr/bin/env python3
"""One-attempt macOS sender for the wx-cli outbox protocol (Python 3.9+).

A private, explicit WX_SENDER_CONFIG binds one channel, one helper binary and
one calibrated window. A UI click alone is never reported as ui_observed.
"""
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

MAX_INPUT = 70 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
BUDGET = 48.0
# Native focus performs guarded search setup before its six-second result
# polling budget, then reconfirms the row and selected header. Allow that whole
# action to finish; an eight-second wait can orphan a still-running helper.
FOCUS_SECONDS = 18.0


class Refused(Exception):
    pass


class Uncertain(Exception):
    pass


def private(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != mode or (not directory and info.st_nlink != 1):
        raise Refused("unsafe private path")


def private_read(path):
    private(path)
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise Refused("unsafe private file")
        raw = stream.read(MAX_OUTPUT + 1)
    if len(raw) > MAX_OUTPUT:
        raise Refused("private file too large")
    return json.loads(raw.decode("utf-8"))


def private_create(path, value):
    private(path.parent, True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def private_update(path, value):
    private(path)
    fd, temp_name = tempfile.mkstemp(prefix=".sender-report-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, str(path))
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def abs_path(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise Refused("absolute canonical path required")
    return Path(value)


def executable(value):
    path = abs_path(value)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not os.access(str(path), os.X_OK):
        raise Refused("regular executable required")
    return path


def validate_text(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 16000 or len(text) > 2000:
        raise Refused("unsupported message text")
    # The observed WeChat flow dropped a terminal line break from the bubble.
    # Refuse it before queueing rather than weakening exact event matching.
    if text.endswith(("\r", "\n")):
        raise Refused("trailing line break is not supported; remove it from message input")
    if any((ord(ch) < 32 and ch not in "\t\r\n") or ord(ch) == 127 for ch in text):
        raise Refused("unsupported message control character")


def validate_empty_composer_reference(config):
    reference = config.get("empty_composer_reference")
    if reference is None:
        return
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise Refused("invalid empty composer reference")
    path = abs_path(reference["path"])
    if path.parent != config["ui_dir"] or path.suffix != ".png":
        raise Refused("empty composer reference must be a private UI PNG")
    private(path)
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refused("unsafe empty composer reference")
        raw = stream.read(2 * 1024 * 1024 + 1)
    rect = config["composer_rect"]
    if (any(rect[k] != int(rect[k]) for k in ("x", "y", "width", "height"))
            or rect["width"] <= 0 or rect["height"] <= 0
            or rect["width"] * rect["height"] > 1048576
            or len(raw) > 2 * 1024 * 1024 or len(raw) < 24
            or raw[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            or struct.unpack(">II", raw[16:24]) != (rect["width"], rect["height"])
            or not isinstance(reference["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])
            or hashlib.sha256(raw).hexdigest() != reference["sha256"]):
        raise Refused("empty composer reference hash or dimensions differ")


def safe_config(config):
    if not isinstance(config, dict) or config.get("protocol_version") != 1:
        raise Refused("unsupported sender configuration")
    binding = config.get("binding")
    if not isinstance(binding, dict) or binding.get("encrypted_snapshot") is not True or not isinstance(binding.get("self_wxid"), str) or not binding["self_wxid"]:
        raise Refused("sender requires encrypted-copy channel and known local identity")
    for key in ("channel_id", "session_id", "account", "talker", "source"):
        if not isinstance(binding.get(key), str) or not binding[key]:
            raise Refused("incomplete sender binding")
    kind = "group" if binding["talker"].endswith("@chatroom") else "private"
    if "target_name" in config:
        if config.get("conversation_kind") != kind:
            raise Refused("conversation kind differs from binding")
        if "target_group" in config and config["target_group"] != config["target_name"]:
            raise Refused("conflicting target names")
        title = config["target_name"]
    else:
        # Preserve the deployed group configuration and transaction identifiers.
        if kind != "group":
            raise Refused("private conversation requires explicit target_name and kind")
        title = config.get("target_group")
    if not isinstance(title, str) or not title or len(title) > 100 or any(ord(c) < 32 for c in title):
        raise Refused("invalid conversation name")
    config["conversation_kind"] = kind
    # The existing signed helper calls this field targetGroup, but accepts a
    # supplied chat title for either kind. Do not rebuild it for this alias.
    config["target_group"] = title
    config["wx_cli"] = executable(config.get("wx_cli"))
    config["channel_state_dir"] = abs_path(config.get("channel_state_dir"))
    private(config["channel_state_dir"], True)
    config["helper_app"] = abs_path(config.get("helper_app"))
    app = config["helper_app"]
    if app.suffix != ".app" or not stat.S_ISDIR(app.lstat().st_mode):
        raise Refused("regular helper app required")
    helper = executable(str(app / "Contents/MacOS/WeChatTestBridge"))
    digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    if not isinstance(config.get("helper_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", config["helper_sha256"]) or digest != config["helper_sha256"]:
        raise Refused("helper executable hash mismatch")
    config["ui_dir"] = abs_path(config.get("ui_dir"))
    if config["ui_dir"] != Path.home() / "wechat-wx-cli-test/ui":
        raise Refused("helper output directory mismatch")
    private(config["ui_dir"].parent, True)
    private(config["ui_dir"], True)
    pid = config.get("expected_pid")
    if type(pid) is not int or pid <= 0:
        raise Refused("expected PID required")
    size = config.get("window_size")
    if not isinstance(size, dict) or any(type(size.get(k)) not in (int, float) or not math.isfinite(size[k]) or size[k] <= 0 for k in ("width", "height")):
        raise Refused("window size required")
    for name, keys in (("composer_rect", ("x", "y", "width", "height")),
                       ("button_point", ("x", "y")),
                       ("conversation_list_rect", ("x", "y", "width", "height")),
                       ("search_point", ("x", "y"))):
        item = config.get(name)
        if not isinstance(item, dict) or any(type(item.get(k)) not in (int, float) or not math.isfinite(item[k]) for k in keys):
            raise Refused("calibrated coordinates required")
    list_rect = config["conversation_list_rect"]
    point = config["search_point"]
    if (list_rect["x"] < 0 or list_rect["y"] < 50 or
        list_rect["width"] < 100 or list_rect["height"] < 100 or
        list_rect["x"] + list_rect["width"] > size["width"] * 0.36 or
        list_rect["y"] + list_rect["height"] > size["height"] - 5 or
        not list_rect["x"] <= point["x"] < list_rect["x"] + list_rect["width"] or
        not 5 <= point["y"] < list_rect["y"] - 5):
        raise Refused("invalid group navigation calibration")
    draft = config.get("expected_draft", "")
    if not isinstance(draft, str) or (draft and (len(draft.encode("utf-8")) > 16000 or len(draft) > 2000)):
        raise Refused("invalid expected draft")
    validate_empty_composer_reference(config)
    return config


def call(argv, deadline, limit=MAX_OUTPUT):
    stop = min(deadline, time.monotonic() + 8.0)
    if stop <= time.monotonic():
        raise Uncertain("command deadline")
    proc = subprocess.Popen([str(x) for x in argv], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    output = bytearray()
    selector = selectors.DefaultSelector()
    try:
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = stop - time.monotonic()
            if remaining <= 0:
                raise Uncertain("command timeout")
            for key, _ in selector.select(min(remaining, 0.2)):
                try:
                    chunk = os.read(key.fileobj.fileno(), min(65536, limit + 1 - len(output)))
                except BlockingIOError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    output.extend(chunk)
                    if len(output) > limit:
                        raise Refused("command output limit exceeded")
        remaining = stop - time.monotonic()
        if remaining <= 0:
            raise Uncertain("command timeout")
        if proc.wait(timeout=remaining) != 0:
            raise Refused("local command failed")
        return output.decode("utf-8")
    except (Refused, Uncertain, subprocess.TimeoutExpired):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        raise
    finally:
        selector.close()
        if proc.stdout:
            proc.stdout.close()


def channel(config, deadline, *args):
    base = [config["wx_cli"], "channel", "--state-dir", config["channel_state_dir"]]
    return call(base + list(args), deadline)


def status(config, deadline):
    value = json.loads(channel(config, deadline, "status"))
    binding = value.get("binding")
    if not isinstance(binding, dict) or any(binding.get(k) != v for k, v in config["binding"].items()):
        raise Refused("channel binding changed")
    if binding.get("encrypted_snapshot") is not True or not binding.get("self_wxid"):
        raise Refused("channel identity unavailable")
    cursor = value.get("latest_cursor")
    if type(cursor) is not int or cursor < 0:
        raise Refused("invalid channel cursor")
    return cursor


def request_ok(request, config, attempt_number=None):
    if not isinstance(request, dict) or request.get("protocol_version") != 1:
        raise Refused("unsupported outbox protocol")
    if attempt_number is None:
        attempt_number = request.get("attempt_number", 1)
    if type(attempt_number) is not int or attempt_number < 1:
        raise Refused("invalid attempt number")
    recovery_from = request.get("recovery_from_attempt")
    if recovery_from is not None and (type(recovery_from) is not int or recovery_from < 1
                                      or attempt_number != recovery_from + 1):
        raise Refused("invalid recovery attempt binding")
    rid = request.get("request_id")
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", rid):
        raise Refused("invalid request ID")
    for key in ("channel_id", "session_id", "account", "talker"):
        if request.get(key) != config["binding"][key]:
            raise Refused("request binding mismatch")
    validate_text(request.get("text"))
    identity = (request["channel_id"] + "\0" + rid) if "target_name" in config else rid
    tid = "send-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]
    return tid if attempt_number == 1 else tid + "-a" + str(attempt_number)


PRE_SUBMIT_ERRORS = {
    "Cannot snapshot a clipboard item",
    "Copied composer text does not exactly match expected draft",
}

CATEGORIES = {"contacts", "group-chats", "most-used"}


def category_matches_kind(category, kind):
    return (category in CATEGORIES and kind in ("group", "private")
            and (category == "most-used" or
                 (category == "contacts" and kind == "private") or
                 (category == "group-chats" and kind == "group")))


def _exists(path):
    return path.exists() or path.is_symlink()


def _read_artifact(path):
    try:
        value = private_read(path)
    except (OSError, ValueError, UnicodeError, Refused) as exc:
        raise Refused("prior attempt evidence is unavailable") from exc
    if not isinstance(value, dict):
        raise Refused("prior attempt evidence is invalid")
    return value


def _prepare_command(request, config, window_id=None, frame=None, focus_id=None):
    command = {"targetGroup": config["target_group"], "expectedDraft": config.get("expected_draft", ""),
            "targetKind": config.get("conversation_kind", "group"),
            "focusEvidenceID": focus_id,
            "text": request["text"], "expectedPID": config["expected_pid"],
            "expectedWindowID": window_id if window_id is not None else config.get("expected_window_id"),
            "expectedFrame": frame if frame is not None else config.get("expected_frame"),
            "composerRect": config["composer_rect"], "buttonPoint": config["button_point"]}
    if config.get("empty_composer_reference") is not None:
        command["emptyComposerReference"] = config["empty_composer_reference"]
    return command


def focus_result_ok(result, config, focus_id, window_id, frame):
    kind = config.get("conversation_kind", "group")
    category = result.get("category")
    if (result.get("focused") is not True or result.get("alreadySelected") is not False
            or result.get("source") != "search" or result.get("targetKind") != kind
            or result.get("focusEvidenceID") != focus_id
            or not category_matches_kind(category, kind)):
        raise Uncertain("kind-aware conversation focus not confirmed")
    evidence = _read_artifact(config["ui_dir"] / (focus_id + ".focus-evidence.json"))
    if (evidence.get("id") != focus_id or evidence.get("targetGroup") != config["target_group"]
            or evidence.get("targetKind") != kind or evidence.get("expectedPID") != config["expected_pid"]
            or evidence.get("expectedWindowID") != window_id or evidence.get("expectedFrame") != frame
            or evidence.get("source") != "search" or evidence.get("category") != category
            or not category_matches_kind(evidence.get("category"), kind)
            or type(evidence.get("selectedAt")) not in (int, float)):
        raise Refused("native focus evidence differs from the requested conversation")
    return {"targetKind": kind, "focusEvidenceID": focus_id,
            "source": "search", "category": category}


def prove_unsent(request, config, attempt_number, deadline, persist=True):
    """Require old native prepare failure and a durable, complete no-submit trace."""
    if type(attempt_number) is not int or attempt_number < 2:
        raise Refused("recovery requires a later attempt")
    recovery_from = request.get("recovery_from_attempt")
    if type(recovery_from) is not int or recovery_from != attempt_number - 1:
        raise Refused("recovery attempt binding mismatch")
    prior_request = dict(request, attempt_number=recovery_from)
    prior_request.pop("recovery_from_attempt", None)
    old_tid = request_ok(prior_request, config, recovery_from)
    ui = config["ui_dir"]
    report_path = ui / (old_tid + ".client-result.json")
    prep_job_path = ui / (old_tid + ".prepare-visual.job.json")
    prep_result_path = ui / (old_tid + ".prepare-visual.result.json")
    state_path = ui / (old_tid + ".visual-state.json")
    submit_paths = (ui / (old_tid + ".submit-attempted.json"),
                    ui / (old_tid + ".submit-visual.job.json"),
                    ui / (old_tid + ".submit-visual.result.json"))
    if any(_exists(path) for path in submit_paths):
        raise Refused("prior attempt has submit evidence")

    report = _read_artifact(report_path)
    job = _read_artifact(prep_job_path)
    result = _read_artifact(prep_result_path)
    state = _read_artifact(state_path)
    if (report.get("id") != old_tid or report.get("stage") != "group_focused"
            or report.get("status") not in ("failed", "indeterminate")):
        raise Refused("prior attempt report does not prove pre-submit stop")
    if (result.get("id") != old_tid or result.get("ok") is not False
            or result.get("error") not in PRE_SUBMIT_ERRORS):
        raise Refused("prior prepare result is not an approved failure")
    permissions = result.get("permissions")
    if not isinstance(permissions, dict) or any(permissions.get(key) is not True
                                                for key in ("accessibility", "screenRecording", "postEvents")):
        raise Refused("prior prepare permissions evidence is incomplete")
    if report.get("error") != helper_reason(result):
        raise Refused("prior attempt report and prepare result differ")
    if (job.get("id") != old_tid or job.get("action") != "prepare-visual"
            or state.get("id") != old_tid or state.get("stage") != "prepare-attempted"):
        raise Refused("prior prepare evidence is inconsistent")
    prior_pid = job.get("expectedPID")
    if type(prior_pid) is not int or prior_pid <= 0:
        raise Refused("prior prepare PID is invalid")
    expected_base = {"targetGroup": config["target_group"],
                     "expectedDraft": config.get("expected_draft", ""),
                     "text": request.get("text"), "expectedPID": prior_pid,
                     "composerRect": config["composer_rect"],
                     "buttonPoint": config["button_point"]}
    for evidence in (job, state):
        if any(evidence.get(key) != value for key, value in expected_base.items()):
            raise Refused("prior attempt text or binding differs")
    if any(state.get(key) != job.get(key) for key in
           ("id", "text", "targetGroup", "expectedDraft", "expectedPID", "expectedWindowID",
            "expectedFrame", "composerRect", "buttonPoint", "emptyComposerReference")):
        raise Refused("prior prepare state differs from its job")
    # A reviewed upgrade may add calibration to an old attempt. Once an attempt
    # used a reference, recovery must retain that exact immutable reference.
    if (job.get("emptyComposerReference") is not None
            and job["emptyComposerReference"] != config.get("empty_composer_reference")):
        raise Refused("prior empty composer reference differs")
    # Inspect and focus must be complete, and every launched job must have a terminal result.
    expected_jobs = {old_tid + ".prepare-visual.job.json",
                     old_tid + "-inspect.inspect.job.json",
                     old_tid + "-focus.focus-group.job.json"}
    actual_jobs = {path.name for path in ui.glob(old_tid + "*.job.json")}
    if actual_jobs != expected_jobs:
        raise Refused("prior attempt has an unexpected or dangling helper job")
    prior_stages = {}
    for suffix, action in (("-inspect", "inspect"), ("-focus", "focus-group")):
        prefix = old_tid + suffix
        launched = list(ui.glob(prefix + "*.job.json"))
        if len(launched) != 1:
            raise Refused("prior attempt has a dangling helper job")
        prior_job = _read_artifact(launched[0])
        result_path = ui / (prefix + ".result.json")
        prior_result = _read_artifact(result_path)
        if (prior_job.get("id") != prefix or prior_job.get("action") != action
                or prior_result.get("id") != prefix or prior_result.get("ok") is not True):
            raise Refused("prior attempt helper stages are inconsistent")
        prior_stages[action] = (prior_job, prior_result)
    if prior_pid != config["expected_pid"]:
        # A normal WeChat restart changes the live PID, not the original
        # attempt. Prove the old PID/window from its own terminal inspect;
        # the new action still requires fresh inspect/focus for the current PID.
        inspected = prior_stages["inspect"][1]
        windows = inspected.get("windows")
        permissions = inspected.get("permissions")
        session = inspected.get("session")
        if (inspected.get("weChatPID") != prior_pid
                or not isinstance(windows, list) or len(windows) != 1
                or not isinstance(windows[0], dict)
                or windows[0].get("id") != job.get("expectedWindowID")
                or windows[0].get("frame") != job.get("expectedFrame")
                or not isinstance(permissions, dict)
                or any(permissions.get(k) is not True for k in
                       ("accessibility", "screenRecording", "postEvents"))
                or not isinstance(session, dict) or session.get("onConsole") is not True
                or session.get("locked") is True):
            raise Refused("prior inspect does not prove the original PID and window")
    focus_id = old_tid + "-focus"
    focus_job, focus_result = prior_stages["focus-group"]
    if (focus_job.get("targetGroup") != config["target_group"]
            or focus_job.get("expectedPID") != prior_pid
            or focus_job.get("expectedWindowID") != job.get("expectedWindowID")
            or focus_job.get("expectedFrame") != job.get("expectedFrame")):
        raise Refused("prior focus request differs from visual transaction")
    focus_evidence_path = ui / (focus_id + ".focus-evidence.json")
    category_fields_present = any(key in item for item in (job, state, focus_job, focus_result)
                                  for key in ("targetKind", "focusEvidenceID", "category"))
    if category_fields_present:
        if (job.get("targetKind") != config.get("conversation_kind", "group")
                or job.get("focusEvidenceID") != focus_id
                or state.get("targetKind") != config.get("conversation_kind", "group")
                or state.get("focusEvidenceID") != focus_id
                or focus_job.get("targetKind") != config.get("conversation_kind", "group")
                or focus_result.get("targetKind") != config.get("conversation_kind", "group")
                or focus_result.get("focusEvidenceID") != focus_id
                or focus_result.get("source") != "search"
                or focus_result.get("alreadySelected") is not False
                or not category_matches_kind(focus_result.get("category"),
                                             config.get("conversation_kind", "group"))):
            raise Refused("prior kind-aware focus evidence is inconsistent")
        evidence = _read_artifact(focus_evidence_path)
        if (evidence.get("id") != focus_id
                or evidence.get("targetGroup") != config["target_group"]
                or evidence.get("targetKind") != config.get("conversation_kind", "group")
                or evidence.get("expectedPID") != prior_pid
                or evidence.get("expectedWindowID") != job.get("expectedWindowID")
                or evidence.get("expectedFrame") != job.get("expectedFrame")
                or evidence.get("source") != "search"
                or evidence.get("category") != focus_result.get("category")
                or not category_matches_kind(evidence.get("category"),
                                             config.get("conversation_kind", "group"))
                or type(evidence.get("selectedAt")) not in (int, float)):
            raise Refused("prior native focus evidence differs from the prepared conversation")
        prior_focus_mode = "kind-aware"
    else:
        if _exists(focus_evidence_path):
            raise Refused("prior focus evidence has an unsupported partial format")
        if (focus_result.get("focused") is not True
                or focus_result.get("alreadySelected") is not False
                or focus_result.get("source") not in ("visible-list", "search")):
            raise Refused("legacy focus result is not a completed navigation")
        prior_focus_mode = "legacy"
    if _helper_is_live(old_tid, deadline):
        raise Refused("prior helper process is still live")
    if not persist:
        return {"request_id": request["request_id"], "from_attempt": recovery_from,
                "attempt_number": attempt_number, "transaction_id": old_tid,
                "result_error": result["error"], "report_stage": report["stage"],
                "report_status": report["status"], "prior_focus_mode": prior_focus_mode}
    claim_path = ui / (old_tid + ".recovery-proof-to-" + str(attempt_number) + ".json")
    proof = {"request_id": request["request_id"], "from_attempt": recovery_from,
             "attempt_number": attempt_number, "transaction_id": old_tid,
             "result_error": result["error"], "report_stage": report["stage"],
             "report_status": report["status"], "prior_focus_mode": prior_focus_mode,
             "proof_saved_at": datetime.now(timezone.utc).isoformat()}
    try:
        private_create(claim_path, proof)
    except FileExistsError as exc:
        raise Refused("recovery proof already exists") from exc
    return str(claim_path)


def _helper_is_live(tid, deadline):
    try:
        listing = call(["/bin/ps", "-axo", "pid=,command="], deadline)
    except (Refused, Uncertain, OSError):
        return True
    return any("WeChatTestBridge" in line for line in listing.splitlines())


def recovery_preflight(request, config):
    """Read-only positive proof before Rust claims the next durable attempt."""
    attempt_number = request.get("attempt_number", 1) if isinstance(request, dict) else 1
    request_ok(request, config, attempt_number)
    ui = config["ui_dir"]
    lock_path = ui / "sender.lock"
    fd = os.open(str(lock_path), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r+") as lock:
        private(lock_path)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("sender busy") from exc
        deadline = time.monotonic() + BUDGET
        status(config, deadline)
        prove_unsent(request, config, attempt_number, deadline, persist=False)
    return {"protocol_version": 1, "recovery_protocol_version": 1, "unsent_proven": True}


def title_matches(observed, target):
    # Match the signed helper's optional member-count suffix conservatively.
    # A private contact can itself be named "Alex (2)"; that must not be
    # mistaken for the group/member-count presentation of "Alex".
    return isinstance(observed, str) and re.fullmatch(
        re.escape(target) + r"(?:\s*\([0-9０-９]+\)|\s*（[0-9０-９]+）)?",
        observed.strip(), flags=re.IGNORECASE) is not None


def title_query(target):
    base = target.strip()
    while True:
        stripped = re.sub(r"\s*(?:\([0-9０-９]+\)|（[0-9０-９]+）)$", "", base).strip()
        if not stripped or stripped == base:
            return base
        base = stripped


def titles_overlap(left, right):
    return isinstance(left, str) and (title_matches(left, right) or title_matches(right, left))


def verify_target(config, deadline):
    """Title-based navigation is allowed only for one fresh metadata identity."""
    binding = config["binding"]
    name = config.get("target_name", config.get("target_group"))
    kind = config.get("conversation_kind", "group")
    raw = call([config["wx_cli"], "conversations", "find", "--state-dir",
                config["channel_state_dir"], "--account", binding["account"],
                "--source", binding["source"], "--name", title_query(name),
                "--all-matches", "--kind", "all"], deadline)
    result = json.loads(raw)
    matches = result.get("matches")
    if not isinstance(matches, list) or not all(isinstance(item, dict) for item in matches):
        raise Refused("invalid conversation metadata")
    matches = [item for item in matches if titles_overlap(item.get("name"), name)]
    if (result.get("match_kind") not in ("exact", "partial")
            or len(matches) != 1
            or matches[0].get("talker") != binding["talker"]
            or matches[0].get("kind") != kind
            or matches[0].get("name") != name):
        raise Refused("conversation title is ambiguous, renamed, or differs from binding")


def helper_reason(result):
    permissions = result.get("permissions")
    if isinstance(permissions, dict) and any(
        permissions.get(key) is False
        for key in ("accessibility", "screenRecording", "postEvents")
    ):
        return "bridge permissions missing"
    error = result.get("error")
    if not isinstance(error, str):
        return "helper rejected action"
    # Use only fixed categories; helper errors must never echo private draft text.
    reasons = (
        ("permission", "bridge permissions missing"),
        ("Accessibility", "bridge permissions missing"),
        ("Screen Recording", "bridge permissions missing"),
        ("Post Events", "bridge permissions missing"),
        ("header", "target group header mismatch"),
        ("group rows", "ambiguous target group rows"),
        ("group absent", "target group not found"),
        ("search input", "search input mismatch"),
        ("draft", "composer draft mismatch"),
        ("clipboard", "clipboard state changed"),
        ("Send button", "Send button not confirmed"),
        ("Send label", "Send button not confirmed"),
        ("window", "WeChat window changed"),
        ("frontmost", "WeChat focus changed"),
        ("console", "console unavailable"),
        ("locked", "console unavailable"),
    )
    lower = error.lower()
    for fragment, summary in reasons:
        if fragment.lower() in lower:
            return summary
    return "helper rejected action"


def bridge(config, tid, action, command, deadline, seconds):
    ui = config["ui_dir"]
    result = ui / (tid + ("." + action if action.endswith("-visual") else "") + ".result.json")
    if result.exists() or result.is_symlink():
        raise Uncertain("helper result already exists")
    job = ui / (tid + "." + action + ".job.json")
    private_create(job, dict(command, id=tid, action=action))
    try:
        call(["/usr/bin/open", "-g", "-n", config["helper_app"], "--args", "--input", job], deadline, limit=65536)
    except Refused as exc:
        raise Uncertain("helper launch result unknown") from exc
    stop = min(deadline, time.monotonic() + seconds)
    while time.monotonic() < stop:
        if result.exists() or result.is_symlink():
            data = private_read(result)
            if not isinstance(data, dict) or data.get("id") != tid or data.get("ok") is not True:
                raise Uncertain(helper_reason(data) if isinstance(data, dict) else "invalid helper result")
            return data
        time.sleep(0.05)
    raise Uncertain("helper action timed out")


def inspect_ok(data, config):
    windows = data.get("windows")
    if data.get("weChatPID") != config["expected_pid"] or not isinstance(windows, list) or len(windows) != 1:
        raise Refused("WeChat process or window changed")
    window = windows[0]
    frame = window.get("frame")
    if type(window.get("id")) is not int or window["id"] <= 0 or not isinstance(frame, dict):
        raise Refused("invalid inspected window")
    if any(type(frame.get(k)) not in (int, float) for k in ("x", "y", "width", "height")):
        raise Refused("invalid inspected frame")
    if any(abs(frame[k] - config["window_size"][k]) > 3 for k in ("width", "height")):
        raise Refused("window size changed")
    session = data.get("session") or {}
    permissions = data.get("permissions") or {}
    if session.get("onConsole") is not True or session.get("locked") is True:
        raise Refused("console unavailable")
    if any(permissions.get(k) is not True for k in ("accessibility", "screenRecording", "postEvents")):
        raise Refused("bridge permissions missing")
    return window["id"], frame


def matching_event(event, request, self_wxid, after):
    if not isinstance(event, dict) or type(event.get("cursor")) is not int or event["cursor"] <= after:
        return False
    if any(event.get(k) != request[k] for k in ("channel_id", "session_id", "account", "talker")):
        return False
    if event.get("kind") != "message_observed" or event.get("direction") != "outgoing":
        return False
    message = event.get("message")
    return (isinstance(message, dict) and message.get("talker") == request["talker"]
            and message.get("sender") == self_wxid and message.get("msg_type") == 1
            and message.get("content") == {"Text": request["text"]})


def observe(config, request, after, deadline, details=None):
    """Poll the encrypted-copy journal; transient snapshot errors never resend."""
    started = time.monotonic()
    stop = min(deadline, started + 20.0)
    cursor = after
    if details is None:
        details = {}
    details.update(sync_failures=0, events_failures=0, rows_examined=0, start_cursor=after)
    try:
        while time.monotonic() < stop:
            try:
                channel(config, stop, "sync")
            except (Refused, Uncertain, OSError, ValueError):
                details["sync_failures"] += 1
            else:
                try:
                    raw = channel(config, stop, "events", "--after", str(cursor), "--limit", "1000")
                    for line in raw.splitlines():
                        event = json.loads(line)
                        details["rows_examined"] += 1
                        if matching_event(event, request, config["binding"]["self_wxid"], after):
                            details["matched_cursor"] = event["cursor"]
                            return event["cursor"]
                        if type(event.get("cursor")) is int and event["cursor"] > cursor:
                            cursor = event["cursor"]
                except (Refused, Uncertain, OSError, ValueError, KeyError, TypeError):
                    details["events_failures"] += 1
            remaining = stop - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.75, remaining))
        return None
    finally:
        details["last_cursor"] = cursor
        details["verification_seconds"] = round(time.monotonic() - started, 3)


def focus_only(config):
    """Select the configured group without reading stdin or touching the outbox."""
    deadline = time.monotonic() + BUDGET
    ui = config["ui_dir"]
    lock_path = ui / "sender.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as lock:
        private(lock_path)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("sender busy") from exc
        if _helper_is_live(None, deadline):
            raise Refused("prior helper process is still live or cannot be inspected")
        status(config, deadline)
        verify_target(config, deadline)
        tid = "focus-" + uuid.uuid4().hex
        inspected = bridge(config, tid + "-inspect", "inspect", {}, deadline, 6.0)
        window_id, frame = inspect_ok(inspected, config)
        command = {"targetGroup": config["target_group"],
                   "targetKind": config.get("conversation_kind", "group"),
                   "expectedPID": config["expected_pid"],
                   "expectedWindowID": window_id, "expectedFrame": frame,
                   "conversationListRect": config["conversation_list_rect"],
                   "searchPoint": config["search_point"]}
        result = bridge(config, tid, "focus-group", command, deadline, FOCUS_SECONDS)
        focus_proof = focus_result_ok(result, config, tid, window_id, frame)
        return {"focused": True, "already_selected": False, "source": "search",
                "target_kind": config.get("conversation_kind", "group"), "focus_evidence_id": tid,
                "category": focus_proof["category"]}


def run(request, config):
    started = time.monotonic()
    deadline = started + BUDGET
    attempt_number = request.get("attempt_number", 1) if isinstance(request, dict) else 1
    tid = request_ok(request, config, attempt_number)
    status(config, deadline)
    ui = config["ui_dir"]
    lock_path = ui / "sender.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as lock:
        private(lock_path)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("sender busy") from exc
        if _helper_is_live(None, deadline):
            raise Refused("prior helper process is still live or cannot be inspected")
        report_path = ui / (tid + ".client-result.json")
        report = {"id": tid, "attempt_number": attempt_number,
                  "request_id": request["request_id"], "stage": "started", "status": "indeterminate",
                  "started_at": datetime.now(timezone.utc).isoformat()}
        if request.get("recovery_from_attempt") is not None:
            report["recovery_from_attempt"] = request["recovery_from_attempt"]
        try:
            private_create(report_path, report)
        except FileExistsError as exc:
            raise Uncertain("request already attempted") from exc
        try:
            if request.get("recovery_from_attempt") is not None:
                report["recovery_proof"] = prove_unsent(request, config, attempt_number, deadline)
                private_update(report_path, report)
            verify_target(config, deadline)
            inspected = bridge(config, tid + "-inspect", "inspect", {}, deadline, 6.0)
            window_id, frame = inspect_ok(inspected, config)
            report["stage"] = "inspected"
            private_update(report_path, report)
            focus_id = tid + "-focus"
            focus_command = {"targetGroup": config["target_group"],
                             "targetKind": config.get("conversation_kind", "group"),
                             "expectedPID": config["expected_pid"],
                             "expectedWindowID": window_id, "expectedFrame": frame,
                             "conversationListRect": config["conversation_list_rect"],
                             "searchPoint": config["search_point"]}
            focused = bridge(config, focus_id, "focus-group", focus_command, deadline, FOCUS_SECONDS)
            focus_proof = focus_result_ok(focused, config, focus_id, window_id, frame)
            command = _prepare_command(request, config, window_id, frame, focus_id)
            report["target_kind"] = config.get("conversation_kind", "group")
            report["focus_evidence_id"] = focus_id
            report["focus_category"] = focus_proof["category"]
            report["stage"] = "group_focused"
            private_update(report_path, report)
            prepared = bridge(config, tid, "prepare-visual", command, deadline, 18.0)
            if prepared.get("prepared") is not True:
                raise Uncertain("prepare not confirmed")
            report["stage"] = "prepared"
            private_update(report_path, report)
            # A fresh encrypted-copy sync establishes the event baseline before Send.
            channel(config, deadline, "sync")
            before = status(config, deadline)
            submit_started = time.monotonic()
            submitted = bridge(config, tid, "submit-visual", command, deadline, 18.0)
            if submitted.get("submitted") is not True:
                raise Uncertain("submit not confirmed")
            report["stage"] = "submit_clicked"
            report["ui_submit_seconds"] = round(time.monotonic() - started, 3)
            report["submit_action_seconds"] = round(time.monotonic() - submit_started, 3)
            private_update(report_path, report)
            observation = {}
            observed_cursor = observe(config, request, before, deadline, observation)
            report["observation"] = observation
            report["verification_seconds"] = observation.get("verification_seconds")
            if observed_cursor is None:
                raise Uncertain("local outgoing row not observed")
            report.update(stage="local_outgoing_observed", status="ui_observed", event_cursor=observed_cursor)
        except Refused as exc:
            # Once the UI may have been touched, a failure cannot prove no Send occurred.
            report.update(status="indeterminate" if report["stage"] != "started" else "failed", error=str(exc))
        except (Uncertain, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            report.update(status="indeterminate", error=str(exc) if isinstance(exc, Uncertain) else type(exc).__name__)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        try:
            private_update(report_path, report)
        except OSError:
            return {"status": "indeterminate", "evidence": str(report_path)}
        return {"status": report["status"], "evidence": str(report_path)}


def main(argv=None):
    os.umask(0o077)
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--recovery-capabilities"]:
        print(json.dumps({"protocol_version": 1, "recovery_protocol_version": 1,
                          "pre_submit_proof_required": True}, separators=(",", ":")))
        return 0
    recovery_preflight_mode = argv == ["--recovery-preflight"]
    focus_mode = argv == ["--focus-only"]
    if argv and not (focus_mode or recovery_preflight_mode):
        print(json.dumps({"focused": False, "reason": "unsupported command"}))
        return 2
    if focus_mode:
        try:
            cfg = os.environ.get("WX_SENDER_CONFIG")
            if not cfg:
                raise Refused("WX_SENDER_CONFIG required")
            config = safe_config(private_read(abs_path(cfg)))
            result = focus_only(config)
            code = 0
        except (Refused, Uncertain, OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, (Refused, Uncertain)) else type(exc).__name__
            result = {"focused": False, "reason": reason}
            code = 1
        print(json.dumps(result, separators=(",", ":")))
        return code
    if recovery_preflight_mode:
        try:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                raise Refused("request too large")
            request = json.loads(raw)
            cfg = os.environ.get("WX_SENDER_CONFIG")
            if not cfg:
                raise Refused("WX_SENDER_CONFIG required")
            config = safe_config(private_read(abs_path(cfg)))
            result = recovery_preflight(request, config)
            print(json.dumps(result, separators=(",", ":")))
            return 0
        except (Refused, Uncertain, OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, (Refused, Uncertain)) else type(exc).__name__
            print(json.dumps({"preflight": False, "reason": reason}, separators=(",", ":")), file=sys.stderr)
            return 1
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise Refused("request too large")
        request = json.loads(raw)
        cfg = os.environ.get("WX_SENDER_CONFIG")
        if not cfg:
            raise Refused("WX_SENDER_CONFIG required")
        config = safe_config(private_read(abs_path(cfg)))
        result = run(request, config)
    except (Refused, Uncertain, OSError, ValueError, KeyError, TypeError) as exc:
        outcome = "indeterminate" if isinstance(exc, Uncertain) else "failed"
        result = {"status": outcome, "evidence": "sender preflight refused: " + type(exc).__name__}
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
