#!/usr/bin/env python3
"""Controlled macOS sender guard probes; never sends a message.

Pass the raw macos_sender_driver.py, never a wrapper that exports a fixed
WX_SENDER_CONFIG. The installed package's raw module is selected by default.
Run `safe` with the reply agent stopped. It clones the private sender config for
PID, size and channel-read faults, then tests an already-attempted outbox ID
with an inert driver. `draft` is a separate operator-supervised UI probe: it
never invokes submit-visual or outbox, and requires a visibly nonempty draft.
"""
import argparse
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import uuid


class ProbeFailure(Exception):
    pass


def validate_raw_driver(path):
    """Reject config-overriding launch wrappers before any fault probe runs."""
    if not path.is_absolute() or path.name != "macos_sender_driver.py":
        raise ProbeFailure("--driver must be the raw macos_sender_driver.py, not a wrapper")
    info = path.lstat()
    from . import macos_sender_driver
    packaged_driver = Path(macos_sender_driver.__file__).resolve()
    if not stat.S_ISREG(info.st_mode) or (not os.access(path, os.X_OK) and path.resolve() != packaged_driver):
        raise ProbeFailure("raw sender driver must be a regular executable file or the installed package module")
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (UnicodeError, SyntaxError) as exc:
        raise ProbeFailure("raw sender driver must be Python source") from exc
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not {"safe_config", "focus_only", "run", "main"}.issubset(names) or "WX_SENDER_CONFIG" not in source:
        raise ProbeFailure("raw sender driver contract missing")
    return path


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise ProbeFailure("unsafe private config")


def private_dir(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProbeFailure("unsafe private directory")


def read_json(path):
    private_file(path)
    return json.loads(path.read_text(encoding="utf-8"))


def command(argv, env=None, timeout=12):
    return subprocess.run([str(x) for x in argv], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=env, timeout=timeout, check=False)


def action_counts(ui):
    return {name: len(list(ui.glob("*." + name + ".job.json")))
            for name in ("focus-group", "prepare-visual", "submit-visual")}


def cloned_config(root, original, changes):
    clone = json.loads(json.dumps(original))
    for key, value in changes.items():
        clone[key] = value
    path = root / ("config-" + uuid.uuid4().hex + ".json")
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(clone, stream, ensure_ascii=False)
    return path


def probe_focus_refusal(driver, ui, config_path, expected_reason):
    before = action_counts(ui)
    env = dict(os.environ, WX_SENDER_CONFIG=str(config_path))
    proc = command([sys.executable, driver, "--focus-only"], env=env, timeout=25)
    try:
        result = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ProbeFailure("focus probe returned invalid JSON") from exc
    after = action_counts(ui)
    if proc.returncode == 0 or result.get("focused") is not False:
        raise ProbeFailure("fault did not refuse focus")
    if result.get("reason") != expected_reason:
        raise ProbeFailure("unexpected refusal category")
    if before != after:
        raise ProbeFailure("fault created a UI action job")
    return {"refused": True, "reason": expected_reason, "new_gui_action_jobs": 0}


def cli_json(config, section, *parts):
    proc = command([config["wx_cli"], section, "--state-dir", config["channel_state_dir"], *parts])
    if proc.returncode != 0:
        raise ProbeFailure("channel or outbox read failed")
    return json.loads(proc.stdout.decode("utf-8"))


def repeat_without_ui(config, scratch, ui):
    items = cli_json(config, "outbox", "list")
    candidates = [item for item in items if item.get("attempt_number", 0) >= 1
                  and item.get("status") in ("ui_observed", "indeterminate", "failed", "recipient_confirmed")]
    if not candidates:
        return {"skipped": "no attempted request available"}
    rid = candidates[-1]["request_id"]
    marker = scratch / "inert-driver-invoked"
    driver = scratch / "inert-driver"
    # If an outbox regression invokes this driver, it only creates a private marker.
    driver.write_text("#!/usr/bin/env python3\nfrom pathlib import Path\nPath(%r).touch()\nprint('{\"status\":\"failed\",\"evidence\":\"inert probe\"}')\n" % str(marker), encoding="utf-8")
    driver.chmod(0o700)
    before = action_counts(ui)
    proc = command([config["wx_cli"], "outbox", "--state-dir", config["channel_state_dir"],
                    "send", "--request-id", rid, "--driver", driver])
    after_items = cli_json(config, "outbox", "list")
    if proc.returncode == 0 or marker.exists() or before != action_counts(ui) or after_items != items:
        raise ProbeFailure("already-attempted request was not rejected before driver invocation")
    return {"refused_before_driver": True, "outbox_unchanged": True, "new_gui_action_jobs": 0}


@contextmanager
def sender_lock(ui):
    lock_path = ui / "sender.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ProbeFailure("unsafe sender lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProbeFailure("sender is busy; stop reply agent for probe") from exc
        yield
    finally:
        os.close(fd)


def check_sender_idle(ui):
    with sender_lock(ui):
        pass


def safe_probes(config, driver):
    validate_raw_driver(driver)
    ui = Path(config["ui_dir"])
    private_dir(ui)
    check_sender_idle(ui)
    with tempfile.TemporaryDirectory(prefix="sender-guard-probe-", dir=str(ui.parent)) as name:
        scratch = Path(name)
        private_dir(scratch)
        wrong_pid = cloned_config(scratch, config, {"expected_pid": 2147483647})
        altered_size = dict(config["window_size"], width=config["window_size"]["width"] + 37)
        wrong_size = cloned_config(scratch, config, {"window_size": altered_size})
        invalid_state = scratch / "empty-state"
        invalid_state.mkdir(mode=0o700)
        failed_read = cloned_config(scratch, config, {"channel_state_dir": str(invalid_state)})
        results = {
            "wrong_pid": probe_focus_refusal(driver, ui, wrong_pid, "WeChat process or window changed"),
            "wrong_size": probe_focus_refusal(driver, ui, wrong_size, "window size changed"),
            "channel_read_failure": probe_focus_refusal(driver, ui, failed_read, "local command failed"),
            "repeat_request": repeat_without_ui(config, scratch, ui),
        }
    return results


def load_driver(path):
    spec = importlib.util.spec_from_file_location("guarded_sender", path)
    if spec is None or spec.loader is None:
        raise ProbeFailure("cannot load sender driver")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def draft_probe(config, driver_path):
    """Never calls submit-visual; proof of prior draft needs operator screenshots."""
    validate_raw_driver(driver_path)
    driver = load_driver(driver_path)
    validated = driver.safe_config(json.loads(json.dumps(config)))
    ui = validated["ui_dir"]
    with sender_lock(ui):
        return _draft_probe_locked(validated, driver)


def _draft_probe_locked(validated, driver):
    ui = validated["ui_dir"]
    deadline = time.monotonic() + 18
    driver.status(validated, deadline)
    tid = "draft-probe-" + uuid.uuid4().hex
    inspected = driver.bridge(validated, tid + "-inspect", "inspect", {}, deadline, 6)
    window_id, frame = driver.inspect_ok(inspected, validated)
    if inspected.get("frontmost") is not True:
        raise ProbeFailure("WeChat must already be frontmost for a draft probe")
    # Capture before and after privately. Do not emit the image or draft content.
    driver.bridge(validated, tid + "-before", "capture", {}, deadline, 4)
    sentinel = "PROBE-EXPECTED-" + uuid.uuid4().hex + uuid.uuid4().hex
    command_data = {"targetGroup": validated["target_group"], "expectedDraft": sentinel,
                    "text": "PROBE-NO-SEND-" + uuid.uuid4().hex,
                    "expectedPID": validated["expected_pid"],
                    "expectedWindowID": window_id, "expectedFrame": frame,
                    "composerRect": validated["composer_rect"],
                    "buttonPoint": validated["button_point"]}
    reason = None
    try:
        driver.bridge(validated, tid, "prepare-visual", command_data, deadline, 8)
    except driver.Uncertain as exc:
        reason = str(exc)
    driver.bridge(validated, tid + "-after", "capture", {}, deadline, 4)
    if reason != "composer draft mismatch":
        raise ProbeFailure("draft guard did not reject as expected; inspect private captures and draft")
    return {"prepare_refused": True, "reason": reason,
            "requires_manual_before_after_draft_check": True,
            "submit_called": False, "outbox_touched": False,
            "private_capture_prefix": str(ui / tid)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("safe", "draft"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--driver", type=Path,
                        help="absolute path to raw macos_sender_driver.py; defaults to installed package source")
    parser.add_argument("--draft-observed", action="store_true",
                        help="required for draft mode after the operator confirms a nonempty composer draft")
    args = parser.parse_args(argv)
    if args.driver is None:
        from . import macos_sender_driver
        args.driver = Path(macos_sender_driver.__file__).resolve()
    os.umask(0o077)
    try:
        config = read_json(args.config)
        private_dir(args.config.parent)
        validate_raw_driver(args.driver)
        if args.mode == "safe":
            result = safe_probes(config, args.driver)
        else:
            if not args.draft_observed:
                raise ProbeFailure("draft mode requires --draft-observed after visual confirmation")
            result = draft_probe(config, args.driver)
        print(json.dumps({"result": "passed", "checks": result}, ensure_ascii=False))
        return 0
    except (ProbeFailure, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, ProbeFailure) else type(exc).__name__
        print(json.dumps({"result": "failed", "reason": reason}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
