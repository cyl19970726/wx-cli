"""Select and use independently bound group or private WeChat conversations."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import uuid

from . import local
from . import dispatch
from . import macos_sender_driver as sender


REGISTRY_NAME = "conversations.json"
LOCK_NAME = "conversations.lock"
TERMINAL = ("ui_observed", "recipient_confirmed")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
CONVERSATION_ID = re.compile(r"[0-9a-f]{32}\Z")
# Registry entry origin for state adopted from the retired single-group runner.
RETIRED_CONTROLLER = "retired_controller"


class ConversationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ConversationError(code, message)


def absolute(value, label):
    require(isinstance(value, str) and Path(value).is_absolute() and ".." not in Path(value).parts,
            "config", label + " must be an absolute canonical path")
    return Path(value)


def config(path):
    require(path.is_absolute(), "config", "absolute --config path required")
    value = local.read_json(path)
    keys = {"version", "wx_cli", "registry_dir", "account", "source", "sender_template",
            "sender_executable", "controller_configs"}
    require(set(value) <= keys and keys - {"controller_configs"} <= set(value) and value["version"] == 1,
            "config", "invalid conversation config")
    for key in ("wx_cli", "registry_dir", "source", "sender_template", "sender_executable"):
        absolute(value[key], key)
    require(isinstance(value["account"], str) and value["account"] and
            Path(value["source"]).name == value["account"], "config", "account/source mismatch")
    # Obsolete: entries imported from the retired single-group runner are
    # migrated with `retire-controller`; listed paths are validated but unused.
    controllers = value.get("controller_configs", [])
    require(isinstance(controllers, list) and all(isinstance(item, str) for item in controllers),
            "config", "controller_configs must be absolute paths")
    for item in controllers:
        absolute(item, "controller config")
    local.private(Path(value["registry_dir"]), True)
    require(Path(value["source"]).is_dir(), "config", "source directory unavailable")
    value["source"] = str(Path(value["source"]).resolve())
    registry_root = Path(value["registry_dir"]).resolve()
    source_root = Path(value["source"])
    require(registry_root != source_root and source_root not in registry_root.parents and
            registry_root not in source_root.parents,
            "config", "registry and source directories must be separate")
    local.read_json(Path(value["sender_template"]))
    for key in ("wx_cli", "sender_executable"):
        executable = Path(value[key])
        require(executable.is_file() and os.access(executable, os.X_OK), "config", key + " unavailable")
    return value


@contextmanager
def registry_lock(settings):
    path = Path(settings["registry_dir"]) / LOCK_NAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        local.private(path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def channel_lock(settings, id):
    require(isinstance(id, str) and bool(CONVERSATION_ID.fullmatch(id)),
            "conversation_id", "invalid conversation ID")
    path = Path(settings["registry_dir"]) / ("channel-" + id + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        local.private(path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def registry_path(settings):
    return Path(settings["registry_dir"]) / REGISTRY_NAME


def registry(settings):
    path = registry_path(settings)
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "account": settings["account"], "source": settings["source"],
                "selected_id": None, "entries": {}}
    value = local.read_json(path)
    require(value.get("version") == 1 and value.get("account") == settings["account"] and
            value.get("source") == settings["source"] and isinstance(value.get("entries"), dict),
            "registry_drift", "conversation registry has different ownership")
    return value


def save_registry(settings, value):
    local.atomic_json(registry_path(settings), value)


def conversation_id(account, talker):
    return hashlib.sha256((account + "\0" + talker).encode("utf-8")).hexdigest()[:32]


def command(settings, section, state_dir=None, *parts, data=None):
    argv = [settings["wx_cli"], section]
    if section == "conversations":
        require(parts and parts[0] == "find", "command", "conversation lookup command required")
        argv.append(parts[0])
        parts = parts[1:]
    if state_dir is not None:
        argv += ["--state-dir", str(state_dir)]
    argv += list(parts)
    try:
        return local.command(argv, data=data, timeout=75)
    except (local.CommandError, OSError, subprocess.SubprocessError) as exc:
        raise ConversationError("command_failed", "local " + section + " command failed") from exc


def one_json(settings, section, state_dir=None, *parts, data=None):
    try:
        return local.one_json(command(settings, section, state_dir, *parts, data=data))
    except local.CommandError as exc:
        raise ConversationError("invalid_output", "local command returned invalid JSON") from exc


def find(settings, name=None, exact=False, id=None, kind="all", all_matches=False):
    require((name is None) != (id is None), "lookup", "provide name or ID")
    require(kind in ("all", "group", "private"), "lookup", "invalid conversation kind")
    require(not all_matches or name is not None and not exact,
            "lookup", "--all-matches requires a name without --exact")
    parts = ["find", "--account", settings["account"], "--source", settings["source"],
             "--kind", kind]
    if id is not None:
        require(isinstance(id, str) and bool(CONVERSATION_ID.fullmatch(id)),
                "lookup", "invalid conversation ID")
        parts += ["--id", id]
    else:
        require(isinstance(name, str) and name.strip(), "lookup", "nonempty name required")
        parts += ["--name", name]
        if exact:
            parts.append("--exact")
        if all_matches:
            parts.append("--all-matches")
    result = one_json(settings, "conversations", Path(settings["registry_dir"]), *parts)
    require(result.get("match_kind") in ("id", "exact", "partial", "none") and
            isinstance(result.get("matches"), list), "invalid_output", "invalid finder response")
    for item in result["matches"]:
        require(isinstance(item, dict) and item.get("kind") in ("group", "private") and
                isinstance(item.get("talker"), str) and isinstance(item.get("name"), str) and
                item.get("conversation_id") == conversation_id(settings["account"], item["talker"]),
                "invalid_output", "invalid conversation identity")
    return result


def fresh(settings, id, entry=None, unique_name=False):
    matches = find(settings, id=id)["matches"]
    require(len(matches) == 1, "identity_drift", "conversation ID is absent or ambiguous")
    current = matches[0]
    if entry is not None:
        require(all(current[key] == entry.get(key) for key in ("conversation_id", "talker", "kind", "name")),
                "identity_drift", "conversation identity or display name changed; select it again")
    if unique_name:
        names = find(settings, name=sender.visual_title_query(current["name"]),
                     kind="all", all_matches=True)["matches"]
        require(any(item["conversation_id"] == id and item["name"] == current["name"] for item in names),
                "identity_drift", "selected display name is absent from fresh metadata")
        collisions = [item for item in names if item["conversation_id"] != id and
                      sender.titles_overlap(item["name"], current["name"])]
        require(not collisions, "ambiguous_name", "display name may identify another conversation")
    return current


def binding_matches(settings, binding, metadata):
    return (binding.get("account") == settings["account"] and
            binding.get("source") == settings["source"] and
            binding.get("talker") == metadata["talker"] and
            binding.get("encrypted_snapshot") is True)


def channel_status(settings, entry, consumer=None):
    parts = ["status"]
    if consumer is not None:
        parts += ["--consumer", consumer]
    result = one_json(settings, "channel", entry["state_dir"], *parts)
    binding = result.get("binding")
    require(isinstance(binding, dict) and binding_matches(settings, binding, entry) and
            binding.get("session_id") == entry["session_id"] and
            binding.get("channel_id") == entry["channel_id"],
            "binding_drift", "registered channel binding changed")
    return result


def selected(settings, id=None):
    with registry_lock(settings):
        current = registry(settings)
    chosen = id if id is not None else current.get("selected_id")
    require(isinstance(chosen, str) and chosen in current["entries"],
            "selection_required", "select a conversation first")
    entry = current["entries"][chosen].copy()
    current["_explicit_id"] = id is not None
    require(entry.get("conversation_id") == chosen, "registry_drift", "conversation registry entry differs")
    require(not entry.get("imported"), "legacy_controller",
            "run `conversations retire-controller --id ID` before using this conversation")
    _check_state_dir(settings, chosen, entry)
    channel_status(settings, entry)
    return current, entry


def assert_selection(settings, snapshot, entry):
    with registry_lock(settings):
        current = registry(settings)
    require((snapshot.get("_explicit_id") or current.get("selected_id") == snapshot.get("selected_id")) and
            current["entries"].get(entry["conversation_id"]) == entry and
            current["entries"].get(entry["conversation_id"], {}).get("revision", 0) == entry.get("revision", 0),
            "selection_drift", "conversation selection changed during operation")


@contextmanager
def selected_channel(settings, id=None):
    snapshot, entry = selected(settings, id)
    with channel_lock(settings, entry["conversation_id"]):
        assert_selection(settings, snapshot, entry)
        yield snapshot, entry


def _private_dir(path):
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700, exist_ok=True)
    local.private(path, True)


def _driver_paths(entry):
    state = Path(entry["state_dir"])
    return state / "conversation-sender.json", state / "conversation-sender"


def prepare_driver(settings, entry):
    state = Path(entry["state_dir"])
    binding = channel_status(settings, entry)["binding"]
    template = local.read_json(Path(settings["sender_template"]))
    template.update(binding=binding, channel_state_dir=str(state), wx_cli=settings["wx_cli"],
                    target_name=entry["name"], conversation_kind=entry["kind"])
    template.pop("target_group", None)
    config_path, driver_path = _driver_paths(entry)
    local.atomic_json(config_path, template)
    command = shlex.quote(settings["sender_executable"])
    if Path(settings["sender_executable"]).name.startswith("python"):
        module = Path(settings["sender_executable"]).parent.parent / "lib"
        versions = sorted(module.glob("python3.*"))
        require(any((version / "site-packages/wx_agent/macos_sender_driver.py").is_file()
                    for version in versions), "driver_drift",
                "candidate Python has no installed sender module")
        command += " -m wx_agent.macos_sender_driver"
    expected = "#!/bin/sh\nexport WX_SENDER_CONFIG=" + shlex.quote(str(config_path)) + "\nexec " + command + " \"$@\"\n"
    if driver_path.exists() or driver_path.is_symlink():
        info = driver_path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                stat.S_IMODE(info.st_mode) == 0o700 and info.st_nlink == 1,
                "driver_drift", "unsafe conversation driver")
        require(driver_path.read_text(encoding="utf-8") == expected,
                "driver_drift", "conversation driver changed")
    else:
        fd = os.open(driver_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
    return str(driver_path)


def _check_state_dir(settings, id, entry):
    """A registry entry owns either its own registry channel or a retired runner's state."""
    expected = Path(settings["registry_dir"]) / "channels" / id
    if entry.get("state_origin") == RETIRED_CONTROLLER:
        state = absolute(entry.get("state_dir"), "retired controller state")
        require(state != expected, "registry_drift", "retired controller state path changed")
        local.private(state, True)
    else:
        require(entry.get("state_dir") == str(expected), "registry_drift", "manual channel path changed")


def _unregistered_legacy_state(settings, metadata):
    """Whether a still-configured legacy controller holds this conversation's channel.

    Selecting it anew would create a second channel for the same conversation,
    detaching the existing history and outbox, so such a selection is refused.
    """
    for path_value in settings.get("controller_configs", []):
        state = local.read_json(Path(path_value)).get("state_dir")
        if not isinstance(state, str) or not Path(state).is_dir():
            continue
        binding = one_json(settings, "channel", state, "status").get("binding")
        if isinstance(binding, dict) and binding_matches(settings, binding, metadata):
            return True
    return False


def retire_controller(settings, id):
    """Turn an entry imported from the retired single-group runner into a plain entry.

    The runner's state directory, channel binding, consumer, outbox and files
    stay where they are; only the registry stops depending on the old runner.
    """
    require(isinstance(id, str) and bool(CONVERSATION_ID.fullmatch(id)),
            "conversation_id", "retire-controller requires an explicit conversation ID")
    with channel_lock(settings, id):
        with registry_lock(settings):
            old = registry(settings)["entries"].get(id)
        require(isinstance(old, dict) and old.get("conversation_id") == id,
                "selection_required", "conversation is not registered")
        if not old.get("imported"):
            return {"retired": False, "conversation_id": id,
                    "state_origin": old.get("state_origin", "registry")}
        state = absolute(old.get("state_dir"), "controller state")
        local.private(state, True)
        controller_path = absolute(old.get("controller_config"), "controller config")
        controller = local.read_json(controller_path)
        require(controller.get("state_dir") == str(state), "controller_drift",
                "controller state directory changed")
        blockers = local.legacy_runner_blockers(state)
        require(not blockers, "legacy_runner_active", "; ".join(blockers))
        entry = dict(old, imported=False, controller_config=None, state_origin=RETIRED_CONTROLLER,
                     retired_controller_config=str(controller_path))
        _check_state_dir(settings, id, entry)
        binding = channel_status(settings, entry)["binding"]
        require(binding_matches(settings, binding, entry), "binding_drift", "channel binding differs")
        entry["revision"] = old.get("revision", 0) + 1
        with registry_lock(settings):
            current = registry(settings)
            require(current["entries"].get(id) == old, "registry_drift", "conversation entry changed")
            current["entries"][id] = entry
            save_registry(settings, current)
    return {"retired": True, "conversation_id": id, "state_dir": str(state),
            "state_origin": RETIRED_CONTROLLER}


def select(settings, id, focus=False):
    metadata = fresh(settings, id)
    with channel_lock(settings, id):
        with registry_lock(settings):
            old = registry(settings)["entries"].get(id)
        if old is not None:
            require(not old.get("imported"), "legacy_controller",
                    "run `conversations retire-controller --id ID` before selecting this conversation")
            _check_state_dir(settings, id, old)
            state_dir = old["state_dir"]
            binding = channel_status(settings, old)["binding"]
        else:
            require(not _unregistered_legacy_state(settings, metadata), "legacy_controller",
                    "a configured legacy controller already owns this conversation; select it with the "
                    "previous release, then run `conversations retire-controller --id ID`")
            channels = Path(settings["registry_dir"]) / "channels"
            _private_dir(channels)
            state = channels / id
            require(not state.exists() and not state.is_symlink(), "state_exists",
                    "unregistered channel state already exists")
            _private_dir(state)
            state_dir = str(state)
            binding = one_json(settings, "channel", state, "bind", "--account", settings["account"],
                               "--talker", metadata["talker"], "--session", "conversation-" + id,
                               "--source", settings["source"], "--encrypted-snapshot")
        require(binding_matches(settings, binding, metadata), "binding_drift", "channel binding differs")
        entry = dict(metadata, state_dir=state_dir, session_id=binding["session_id"],
                     channel_id=binding["channel_id"], imported=False, controller_config=None)
        for key in ("state_origin", "retired_controller_config"):
            if old is not None and key in old:
                entry[key] = old[key]
        if old is not None:
            require(old["state_dir"] == state_dir and old["session_id"] == entry["session_id"] and
                    old["channel_id"] == entry["channel_id"], "registry_drift", "selection would rebind existing state")
        entry["revision"] = (old.get("revision", 0) + 1 if old is not None and
                             any(old.get(key) != value for key, value in entry.items()) else
                             old.get("revision", 0) if old is not None else 1)
        with registry_lock(settings):
            current = registry(settings)
            require(current["entries"].get(id) == old, "registry_drift", "conversation entry changed")
            current["entries"][id] = entry
            current["selected_id"] = id
            save_registry(settings, current)
    if focus:
        return focus_conversation(settings, id)
    return {"selected": True, "conversation": metadata, "state_dir": state_dir,
            "state_origin": entry.get("state_origin", "registry")}


def status(settings, id=None):
    _, entry = selected(settings, id)
    channel = channel_status(settings, entry)
    result = {"conversation": {key: entry[key] for key in ("conversation_id", "talker", "name", "kind")},
              "binding": channel["binding"], "latest_cursor": channel["latest_cursor"],
              "history_complete": False, "state_origin": entry.get("state_origin", "registry")}
    return result


def configure(settings, id, owner, input_mode="pull", reply_policy="observe", expected_epoch=None):
    require(isinstance(id, str) and bool(CONVERSATION_ID.fullmatch(id)),
            "conversation_id", "configure requires an explicit conversation ID")
    require(input_mode in ("pull", "push") and reply_policy in ("observe", "draft", "authorized"),
            "configuration", "invalid input mode or reply policy")
    require(expected_epoch is None or type(expected_epoch) is int and expected_epoch >= 0,
            "epoch", "expected epoch must be nonnegative")
    with selected_channel(settings, id) as (snapshot, entry):
        fresh(settings, id, entry)
        assert_selection(settings, snapshot, entry)
        return dispatch.configure(settings, entry, owner, input_mode, reply_policy, expected_epoch)


def control(settings, id, action, expected_epoch):
    require(isinstance(id, str) and bool(CONVERSATION_ID.fullmatch(id)),
            "conversation_id", action + " requires an explicit conversation ID")
    require(type(expected_epoch) is int and expected_epoch >= 0,
            "epoch", "expected epoch must be nonnegative")
    require(action in ("takeover", "handback"), "command", "invalid control action")
    with selected_channel(settings, id) as (snapshot, entry):
        root = _managed_root(settings, entry)
        require(root is not None, "not_managed", "conversation is not managed")
        assert_selection(settings, snapshot, entry)
        return dispatch.call(settings["wx_cli"], root, action, "--conversation-id", id,
                             "--expected-epoch", expected_epoch)


def _managed_root(settings, entry):
    root = dispatch.managed_root(entry["state_dir"])
    if root is None:
        desktop=Path(local.read_json(Path(settings["sender_template"]))["ui_dir"])
        require(not (desktop/"coordinator.sqlite3").exists(),"managed_desktop",
                "desktop is managed; configure this conversation before GUI operations")
    if root is not None:
        require(root == dispatch.root_from_settings(settings), "coordinator_drift",
                "managed conversation uses another desktop root")
    return root


def _managed_fence(settings, entry, owner, expected_epoch, root):
    require(isinstance(owner, str) and bool(owner) and
            type(expected_epoch) is int and expected_epoch >= 0,
            "owner_epoch_required", "managed send requires --owner and --expected-epoch")
    binding = dispatch.call(settings["wx_cli"], root, "status", "--conversation-id",
                            entry["conversation_id"])
    require(binding.get("channel_state_dir") == entry["state_dir"] and
            binding.get("channel_id") == entry["channel_id"] and
            binding.get("session_id") == entry["session_id"] and
            binding.get("owner") == owner and binding.get("epoch") == expected_epoch and
            binding.get("control") == "active",
            "owner_epoch_drift", "managed conversation owner, epoch, or binding changed")


def events(settings, entry, after, limit):
    raw = command(settings, "channel", entry["state_dir"], "events", "--after", str(after),
                  "--limit", str(limit))
    try:
        items = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise ConversationError("invalid_output", "invalid event lines") from exc
    last = after
    for item in items:
        require(isinstance(item, dict) and type(item.get("cursor")) is int and item["cursor"] > last and
                item.get("channel_id") == entry["channel_id"] and
                item.get("session_id") == entry["session_id"] and
                item.get("account") == settings["account"] and
                item.get("talker") == entry["talker"] and
                isinstance(item.get("message"), dict) and
                item["message"].get("talker") == entry["talker"],
                "event_drift", "event identity differs")
        last = item["cursor"]
    return items


def read(settings, after=None, limit=50, id=None):
    with selected_channel(settings, id) as (_, entry):
        require(type(limit) is int and 1 <= limit <= 999, "limit", "limit must be 1..999")
        require(after is None or type(after) is int and after >= 0, "cursor", "after must be nonnegative")
        before = channel_status(settings, entry)["binding"]
        one_json(settings, "channel", entry["state_dir"], "sync")
        channel = channel_status(settings, entry)
        require(channel["binding"]["channel_id"] == before["channel_id"],
                "binding_drift", "channel changed during sync")
        cursor = 0 if after is None else after
        page = events(settings, entry, cursor, limit + 1)
        visible = page[:limit]
        return {"conversation": {key: entry[key] for key in ("conversation_id", "talker", "name", "kind")},
                "after": cursor, "next_cursor": visible[-1]["cursor"] if visible else cursor,
                "events": visible, "has_more": len(page) > limit,
                "latest_cursor": channel["latest_cursor"], "history_complete": False}


def send_intent(settings, entry, request_id, text, snapshot=None, media=None,
                owner=None, expected_epoch=None, scheduling=None, wait=True, publisher=None):
    require(isinstance(request_id, str) and bool(REQUEST_ID.fullmatch(request_id)),
            "request_id", "invalid request ID")
    require(publisher is None or (isinstance(publisher, str) and
            bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", publisher))),
            "publisher", "invalid application publisher")
    if media is None:
        sender.validate_text(text)
    if snapshot is not None:
        assert_selection(settings, snapshot, entry)
    state = entry["state_dir"]
    root = _managed_root(settings, entry)
    managed = root is not None
    if managed:
        _managed_fence(settings, entry, owner, expected_epoch, root)
    try:
        listed = json.loads(command(settings, "outbox", state, "list"))
    except (ValueError, UnicodeError) as exc:
        raise ConversationError("invalid_output", "invalid outbox list") from exc
    require(isinstance(listed, list) and all(isinstance(item, dict) for item in listed),
            "invalid_output", "invalid outbox list")
    if not any(item.get("request_id") == request_id for item in listed):
        queue_args = ("queue-image", "--image", media["path"], "--expected-sha256", media["sha256"]) if media else ("queue", "--text-stdin")
        fence_args = ("--owner", owner, "--expected-epoch", str(expected_epoch)) if managed else ()
        schedule_args = tuple(value for key, item in (scheduling or {}).items()
                              if item is not None for value in ("--" + key.replace("_", "-"), str(item)))
        require(not schedule_args or managed, "not_managed", "batch/priority/expiry require a managed conversation")
        one_json(settings, "outbox", state, *queue_args, "--request-id", request_id,
                 "--channel-id", entry["channel_id"], "--session-id", entry["session_id"],
                 *(("--publisher", publisher) if publisher is not None else ()),
                 *fence_args, *schedule_args,
                 data=None if media else text)
    item = one_json(settings, "outbox", state, "show", "--request-id", request_id)
    require(item.get("request_id") == request_id, "outbox_drift", "outbox request ID differs")
    _same_intent(item, text, media)
    require(item.get("publisher_id") == publisher, "publisher_conflict",
            "request originated from another application publisher")
    if managed:
        require(item.get("managed_owner") == owner and item.get("managed_epoch") == expected_epoch,
                "owner_epoch_drift", "outbox intent originated under another owner epoch")
        for key, value in (scheduling or {}).items():
            require(item.get(key) == value, "schedule_conflict", "request has different batch, priority or expiry")
    if item.get("status") in TERMINAL:
        return {"request_id": request_id, "status": item["status"], "already_completed": True}
    expected_status = ("managed_media_queued" if media else "managed_queued") if managed else (
        "media_queued" if media else "queued")
    require(item.get("status") == expected_status,
            "send_uncertain", "send was attempted or outcome is unresolved")
    # Selection and metadata are checked immediately before the desktop effect.
    fresh(settings, entry["conversation_id"], entry, unique_name=True)
    if snapshot is not None:
        assert_selection(settings, snapshot, entry)
    one_json(settings, "channel", state, "sync")
    channel_status(settings, entry)
    driver = prepare_driver(settings, entry)
    if managed:
        _managed_fence(settings, entry, owner, expected_epoch, root)
        if not wait:
            _, queued = dispatch.enqueue_existing(settings["wx_cli"], state, request_id,
                                                  owner=owner, expected_epoch=expected_epoch)
            return {"request_id": request_id, "status": "pending", "dispatch_id": queued["dispatch_id"],
                    "already_completed": False}
        result = dispatch.send_existing(settings["wx_cli"], state, request_id,
                                        owner=owner, expected_epoch=expected_epoch)
        require(result.get("request_id") == request_id, "outbox_drift", "dispatch request ID differs")
        if result.get("status") == "pending":
            return {"request_id": request_id, "status": "pending", "dispatch_id": result.get("dispatch_id"),
                    "already_completed": False}
        _same_intent(result, text, media)
        require(result.get("status") in TERMINAL, "send_uncertain", "send outcome needs manual inspection")
        return {"request_id": request_id, "status": result["status"], "already_completed": False}
    try:
        one_json(settings, "outbox", state, "send", "--request-id", request_id, "--driver", driver)
    except ConversationError:
        # The durable attempt may already have reached WeChat. Inspect only.
        pass
    item = one_json(settings, "outbox", state, "show", "--request-id", request_id)
    _same_intent(item, text, media)
    require(item.get("status") in TERMINAL, "send_uncertain", "send outcome needs manual inspection")
    return {"request_id": request_id, "status": item["status"], "already_completed": False}


def _same_intent(item, text, media):
    if media is None:
        require(item.get("media") is None and item.get("text") == text,
                "text_conflict", "existing request has different text or content type")
    else:
        require(item.get("text") in (None, "") and sender.same_media(item.get("media"), media),
                "image_conflict", "existing request has different image content")


def send_image(settings, request_id, image, id=None, owner=None, expected_epoch=None, scheduling=None, wait=True, publisher=None):
    media = sender.image_descriptor(image)
    return send(settings, request_id, None, id, media=media,
                owner=owner, expected_epoch=expected_epoch, scheduling=scheduling, wait=wait, publisher=publisher)


def send(settings, request_id, text, id=None, media=None, owner=None, expected_epoch=None, scheduling=None, wait=True, publisher=None):
    if media is None:
        sender.validate_text(text)
    with selected_channel(settings, id) as (snapshot, entry):
        fresh(settings, entry["conversation_id"], entry, unique_name=True)
        return send_intent(settings, entry, request_id, text, snapshot, media,
                           owner=owner, expected_epoch=expected_epoch, scheduling=scheduling, wait=wait,
                           publisher=publisher)


def reply(settings, cursor, text, id=None, owner=None, expected_epoch=None):
    with selected_channel(settings, id) as (snapshot, entry):
        require(type(cursor) is int and cursor > 0, "cursor", "positive cursor required")
        sender.validate_text(text)
        fresh(settings, entry["conversation_id"], entry, unique_name=True)
        one_json(settings, "channel", entry["state_dir"], "sync")
        page = events(settings, entry, cursor - 1, 1)
        require(len(page) == 1 and page[0]["cursor"] == cursor, "cursor_missing", "reply source is absent")
        event = page[0]
        body = event.get("message", {}).get("content")
        require(event.get("kind") == "message_observed" and event.get("direction") == "incoming" and
                isinstance(body, dict) and isinstance(body.get("Text"), str) and
                type(event.get("server_id")) is int and event["server_id"] > 0 and
                event.get("message", {}).get("server_id") == event["server_id"] and
                event.get("message", {}).get("talker") == entry["talker"],
                "not_replyable", "cursor is not an incoming observed text")
        request_id = local.reply_request_id(entry["channel_id"], event["server_id"])
        result = send_intent(settings, entry, request_id, text, snapshot,
                             owner=owner, expected_epoch=expected_epoch)
        result["cursor"] = cursor
        # A generic manual read/reply does not own a consumer cursor.
        return result


def recover_unsent_send(settings, request_id, expected_attempt, id=None):
    """Recover one existing outbox intent for a freshly verified stable conversation ID."""
    require(isinstance(id, str) and bool(CONVERSATION_ID.fullmatch(id)),
            "conversation_id_required", "recovery requires a stable conversation ID")
    require(isinstance(request_id, str) and bool(REQUEST_ID.fullmatch(request_id)),
            "request_id", "invalid request ID")
    require(type(expected_attempt) is int and expected_attempt > 0,
            "attempt", "positive expected attempt required")
    with selected_channel(settings, id) as (snapshot, entry):
        fresh(settings, entry["conversation_id"], entry, unique_name=True)
        require(_managed_root(settings, entry) is None, "managed_recovery_unsupported",
                "managed recovery requires coordinator review")
        assert_selection(settings, snapshot, entry)
        item = one_json(settings, "outbox", entry["state_dir"], "show", "--request-id", request_id)
        require(item.get("request_id") == request_id, "outbox_drift", "outbox request ID differs")
        require(isinstance(item.get("text"), str), "outbox_drift", "outbox text is unavailable")
        require(item.get("media") is None, "image_recovery_unsupported", "image attempts cannot be recovered")
        sender.validate_text(item["text"])
        require(item.get("status") in ("failed", "indeterminate"),
                "send_uncertain", "only a failed or indeterminate intent can be recovered")
        require(item.get("attempt_number") == expected_attempt,
                "attempt_drift", "outbox attempt differs from the requested recovery")
        fresh(settings, entry["conversation_id"], entry, unique_name=True)
        assert_selection(settings, snapshot, entry)
        one_json(settings, "channel", entry["state_dir"], "sync")
        channel_status(settings, entry)
        driver = prepare_driver(settings, entry)
        try:
            one_json(settings, "outbox", entry["state_dir"], "recover-unsent", "--request-id", request_id,
                     "--expected-attempt", str(expected_attempt), "--driver", driver)
        except ConversationError:
            # The claim may have been committed. Inspect its immutable current state only.
            pass
        result = one_json(settings, "outbox", entry["state_dir"], "show", "--request-id", request_id)
        require(result.get("request_id") == request_id and result.get("text") == item["text"],
                "outbox_drift", "recovered intent differs from the original")
        require(result.get("status") in TERMINAL, "send_uncertain", "recovery result needs manual inspection")
        return {"request_id": request_id, "attempt_number": result.get("attempt_number"),
                "status": result["status"], "already_completed": False}


def focus_conversation(settings, id=None):
    with selected_channel(settings, id) as (snapshot, entry):
        fresh(settings, entry["conversation_id"], entry, unique_name=True)
        one_json(settings, "channel", entry["state_dir"], "sync")
        driver = prepare_driver(settings, entry)
        assert_selection(settings, snapshot, entry)
        root = _managed_root(settings, entry)
        if root is not None:
            binding = dispatch.call(settings["wx_cli"], root, "status", "--conversation-id",
                                    entry["conversation_id"])
            require(binding.get("channel_state_dir") == entry["state_dir"] and
                    binding.get("channel_id") == entry["channel_id"] and
                    binding.get("session_id") == entry["session_id"],
                    "coordinator_drift", "managed conversation binding changed")
            operation_id = "focus-" + uuid.uuid4().hex
            queued = dispatch.call(settings["wx_cli"], root, "enqueue-focus", "--conversation-id",
                                   entry["conversation_id"], "--operation-id", operation_id,
                                   "--expected-epoch", binding["epoch"])
            dispatch.drain(settings["wx_cli"], root)
            queue = dispatch.call(settings["wx_cli"], root, "dispatch", "status")
            matching = [item for item in queue.get("dispatches", [])
                        if item.get("dispatch_id") == queued.get("dispatch_id")]
            require(len(matching) == 1, "dispatch_drift", "focus dispatch is unavailable")
            item = matching[0]
            if item.get("status") == "done" and item.get("result") == "focus_completed":
                return {"focused": True, "dispatch_id": item["dispatch_id"]}
            return {"focused": False, "status": "pending", "dispatch_id": item["dispatch_id"]}
        try:
            return local.one_json(local.command([driver, "--focus-only"], timeout=75))
        except (local.CommandError, OSError, subprocess.SubprocessError) as exc:
            raise ConversationError("focus_failed", "desktop focus was not confirmed") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wx-agent conversations", description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    lookup = commands.add_parser("find")
    query = lookup.add_mutually_exclusive_group(required=True)
    query.add_argument("--name")
    query.add_argument("--id")
    lookup.add_argument("--exact", action="store_true")
    lookup.add_argument("--kind", choices=("all", "group", "private"), default="all")
    retiring = commands.add_parser("retire-controller",
                                   help="stop depending on a retired single-group runner entry")
    retiring.add_argument("--id", required=True)
    choose = commands.add_parser("select")
    choose.add_argument("--id", required=True)
    choose.add_argument("--focus", action="store_true")
    configuring = commands.add_parser("configure")
    configuring.add_argument("--id", required=True)
    configuring.add_argument("--owner", required=True)
    configuring.add_argument("--input", choices=("pull", "push"), default="pull")
    configuring.add_argument("--reply-policy", choices=("observe", "draft", "authorized"), default="observe")
    configuring.add_argument("--expected-epoch", type=int)
    for name in ("takeover", "handback"):
        controlling = commands.add_parser(name)
        controlling.add_argument("--id", required=True)
        controlling.add_argument("--expected-epoch", type=int, required=True)
    for name in ("status", "read", "focus", "send", "send-image", "reply"):
        commands.add_parser(name).add_argument("--id")
    reading = commands.choices["read"]
    reading.add_argument("--after", type=int)
    reading.add_argument("--limit", type=int, default=50)
    sending = commands.choices["send"]
    sending.add_argument("--request-id", required=True)
    sending.add_argument("--text-stdin", action="store_true", required=True)
    image_sending = commands.choices["send-image"]
    image_sending.description = "Send one local PNG/JPEG once; max 16 MiB, 8192 px per side, 40M pixels."
    image_sending.add_argument("--request-id", required=True)
    image_sending.add_argument("--image", type=Path, required=True)
    replying = commands.choices["reply"]
    replying.add_argument("--cursor", type=int, required=True)
    replying.add_argument("--text-stdin", action="store_true", required=True)
    for name in ("send", "send-image", "reply"):
        commands.choices[name].add_argument("--owner")
        commands.choices[name].add_argument("--expected-epoch", type=int)
    for name in ("send", "send-image"):
        commands.choices[name].add_argument("--publisher")
        commands.choices[name].add_argument("--no-wait", action="store_true")
        commands.choices[name].add_argument("--batch-id")
        commands.choices[name].add_argument("--part-index", type=int)
        commands.choices[name].add_argument("--priority", type=int, choices=(0, 1, 2))
        commands.choices[name].add_argument("--expires-at", type=int)
    commands.add_parser("recover-unsent-send")
    recovering = commands.choices["recover-unsent-send"]
    recovering.add_argument("--id", required=True)
    recovering.add_argument("--request-id", required=True)
    recovering.add_argument("--expected-attempt", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        settings = config(args.config)
        if args.command == "find":
            require(not args.exact or args.name is not None, "lookup", "--exact requires --name")
            result = find(settings, args.name, args.exact, args.id, args.kind)
        elif args.command == "retire-controller":
            result = retire_controller(settings, args.id)
        elif args.command == "select":
            result = select(settings, args.id, args.focus)
        elif args.command == "configure":
            result = configure(settings, args.id, args.owner, args.input, args.reply_policy, args.expected_epoch)
        elif args.command in ("takeover", "handback"):
            result = control(settings, args.id, args.command, args.expected_epoch)
        elif args.command == "status":
            result = status(settings, args.id)
        elif args.command == "read":
            result = read(settings, args.after, args.limit, args.id)
        elif args.command == "focus":
            result = focus_conversation(settings, args.id)
        elif args.command == "recover-unsent-send":
            require(args.id is not None, "conversation_id_required",
                    "recovery requires a stable conversation --id")
            result = recover_unsent_send(settings, args.request_id, args.expected_attempt, args.id)
        elif args.command == "send-image":
            result = send_image(settings, args.request_id, args.image, args.id,
                                owner=args.owner, expected_epoch=args.expected_epoch, wait=not args.no_wait,
                                publisher=args.publisher,
                                scheduling={key: getattr(args, key) for key in ("batch_id", "part_index", "priority", "expires_at") if getattr(args,key) is not None})
        elif args.command == "send":
            raw = sys.stdin.buffer.read(local.MAX_REPLY + 1)
            require(len(raw) <= local.MAX_REPLY, "text", "message text too large")
            result = send(settings, args.request_id, raw.decode("utf-8"), args.id,
                          owner=args.owner, expected_epoch=args.expected_epoch, wait=not args.no_wait,
                          publisher=args.publisher,
                          scheduling={key: getattr(args,key) for key in ("batch_id", "part_index", "priority", "expires_at") if getattr(args,key) is not None})
        elif args.command == "reply":
            raw = sys.stdin.buffer.read(local.MAX_REPLY + 1)
            require(len(raw) <= local.MAX_REPLY, "text", "message text too large")
            result = reply(settings, args.cursor, raw.decode("utf-8"), args.id,
                           owner=args.owner, expected_epoch=args.expected_epoch)
        else:
            raise ConversationError("command", "unsupported conversation command")
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ConversationError, dispatch.DispatchError, local.StateError, local.CommandError,
            sender.Refused, OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, ConversationError) else "local_state_error"
        print(json.dumps({"error": {"code": code, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
