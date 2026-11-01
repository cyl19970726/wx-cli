"""One durable desktop queue; message contents and attempts remain in Rust."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import channel_agent as runner
from . import wechat_agent_ctl as ctl


class DispatchError(RuntimeError):
    pass


def call(binary, root, *parts, data=None):
    return runner.one_json(runner.command(
        [str(binary), "coordinator", "--state-dir", str(root), *map(str, parts)],
        data=data, timeout=90))


def managed_root(state):
    marker = Path(state) / "managed-coordinator.json"
    if not marker.exists():
        return None
    value = ctl.read_json(marker)
    root = Path(value["root"])
    if value.get("version") != 1 or not root.is_absolute() or root.resolve() != root:
        raise DispatchError("invalid coordinator identity")
    ctl.private(root, True)
    return root


@contextmanager
def exclusive(root, name, blocking=False):
    path = Path(root) / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        ctl.private(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise DispatchError("desktop executor busy; queued work is retained")
        yield fd
    finally:
        os.close(fd)


def resolve_binding(binary, root, state):
    state = str(Path(state).resolve())
    for cid in call(binary, root, "status")["conversations"]:
        binding = call(binary, root, "status", "--conversation-id", cid)
        if binding["channel_state_dir"] == state:
            return binding
    raise DispatchError("channel is not enrolled in this coordinator")


def enqueue_existing(binary, state, request_id, owner=None, expected_epoch=None):
    root = managed_root(state)
    if root is None:
        raise DispatchError("channel has no managed desktop binding")
    binding = resolve_binding(binary, root, state)
    if owner != binding["owner"] or expected_epoch != binding["epoch"]:
        raise DispatchError("managed send requires the originating owner and current expected epoch")
    return root, call(binary, root, "enqueue", "--conversation-id", binding["conversation_id"],
                      "--request-id", request_id, "--expected-epoch", expected_epoch)


def _outbox(binary, state, *parts):
    return runner.one_json(runner.command(
        [str(binary), "outbox", "--state-dir", str(state), *map(str, parts)], timeout=90))


def execute_one(binary, root, claim):
    """Never retry an effect. Rust completion derives outcome from the outbox."""
    state = Path(claim["channel_state_dir"])
    driver = state / "conversation-sender"
    if not driver.is_file() or not os.access(driver, os.X_OK):
        call(binary, root, "dispatch", "pause", "--reason", "registered driver unavailable")
        extra = ["--focus-result", "failed"] if claim["operation"] == "focus" else []
        return call(binary, root, "dispatch", "complete", "--dispatch-id", claim["dispatch_id"],
                    "--token", claim["token"], *extra)
    focus_result = None
    try:
        if claim["operation"] == "focus":
            # A focus job shares the identical desktop lock with Rust sends.
            with exclusive(root, "sender.lock") as fd:
                call(binary, root, "dispatch", "validate", "--dispatch-id", claim["dispatch_id"],
                     "--token", claim["token"])
                env = dict(os.environ, WX_DESKTOP_LOCK_FD=str(fd))
                result = subprocess.run([str(driver), "--focus-only"], env=env,
                                        pass_fds=(fd,), capture_output=True, timeout=90)
                value = json.loads(result.stdout)
                if result.returncode or value.get("focused") is not True:
                    raise DispatchError("focus did not complete")
                focus_result = "success"
        else:
            _outbox(binary, state, "managed-send", "--request-id", claim["request_id"],
                    "--driver", driver, "--root", root, "--dispatch-id", claim["dispatch_id"],
                    "--token", claim["token"])
    except (runner.RunnerError, OSError, subprocess.SubprocessError, ValueError, DispatchError):
        # No retry here, including when transport died after a successful click.
        if claim["operation"] == "focus":
            focus_result = "failed"
    extra = ["--focus-result", focus_result] if focus_result is not None else []
    return call(binary, root, "dispatch", "complete", "--dispatch-id", claim["dispatch_id"],
                "--token", claim["token"], *extra)


def drain(binary, root, max_items=100):
    results = []
    try:
        with exclusive(root, "executor.lock"):
            worker = "executor-" + uuid.uuid4().hex
            for _ in range(max_items):
                claim = call(binary, root, "dispatch", "claim", "--worker", worker)
                if claim.get("status") != "claimed":
                    return {"status": claim.get("status"), "results": results, "queue": claim}
                result = execute_one(binary, root, claim)
                results.append(result)
                if result.get("surface_paused") or result.get("status") in ("paused", "queued"):
                    break
    except DispatchError:
        return {"status": "busy", "results": results}
    return {"status": "bounded", "results": results}


def reconcile(binary, root):
    results=[]
    for cid in call(binary,root,"status")["conversations"]:
        try:
            results.append(call(binary,root,"reconcile-outbox","--conversation-id",cid))
        except (runner.RunnerError,OSError):
            results.append({"conversation_id":cid,"error":"outbox reconciliation requires inspection"})
    return results


def send_existing(binary, state, request_id, wait_seconds=90, owner=None, expected_epoch=None):
    root, queued = enqueue_existing(binary, state, request_id, owner, expected_epoch)
    deadline = time.monotonic() + wait_seconds
    while True:
        drain(binary, root)
        item = _outbox(binary, state, "show", "--request-id", request_id)
        if item["status"] not in ("managed_queued", "managed_media_queued"):
            return item
        status = call(binary, root, "dispatch", "status")
        if status.get("paused_reason") or time.monotonic() >= deadline:
            return {"request_id": request_id, "status": "pending", "dispatch_id": queued["dispatch_id"]}
        time.sleep(0.2)


def root_from_settings(settings):
    template = ctl.read_json(Path(settings["sender_template"]))
    root = Path(template["ui_dir"])
    ctl.private(root, True)
    if root.resolve() != root:
        raise DispatchError("desktop root must be canonical")
    return root


def configure(settings, entry, owner, input_mode="pull", reply_policy="observe", expected_epoch=None):
    from . import conversations
    root = root_from_settings(settings)
    if expected_epoch is None and entry.get("imported"):
        controller = ctl.config(Path(entry["controller_config"]), require_executables=False)
        if ctl.status(controller)["alive"]:
            ctl.action(controller, "stop", 90)
        state = Path(entry["state_dir"])
        if (state / ctl.INTERACTIVE_NAME).exists():
            raise DispatchError("finish the existing interactive lease before migration")
        original = state / runner.STATE_NAME
        if original.exists() and runner.load(original).get("pending") is not None:
            raise DispatchError("resolve the original pending reply before migration")
    conversations.prepare_driver(settings, entry)
    parts = ["bind", "--channel-state-dir", entry["state_dir"]] if expected_epoch is None else ["configure", "--expected-epoch", expected_epoch]
    result = call(settings["wx_cli"], root, *parts, "--conversation-id", entry["conversation_id"],
                  "--owner", owner, "--input", input_mode, "--reply-policy", reply_policy)
    _outbox(settings["wx_cli"], entry["state_dir"], "manage", "--root", root)
    call(settings["wx_cli"], root, "reconcile-outbox", "--conversation-id", entry["conversation_id"])
    return result


def review_surface(binary, root, reason, confirm_desktop_safe, restore_unattempted=None):
    if not confirm_desktop_safe:
        raise DispatchError("review requires explicit confirmation that the desktop draft is safe")
    with exclusive(root, "executor.lock"), exclusive(root, "sender.lock") as fd:
        ids = call(binary, root, "status")["conversations"]
        if not ids:
            raise DispatchError("no registered desktop driver")
        binding = call(binary, root, "status", "--conversation-id", ids[0])
        driver = Path(binding["channel_state_dir"]) / "conversation-sender"
        result = subprocess.run([str(driver), "--dispatch-preflight"],
                                env=dict(os.environ, WX_DESKTOP_LOCK_FD=str(fd)), pass_fds=(fd,),
                                capture_output=True, timeout=30)
        if result.returncode or json.loads(result.stdout).get("ready") is not True:
            raise DispatchError("fresh desktop/helper preflight did not pass")
        if restore_unattempted is not None:
            call(binary,root,"dispatch","restore-queued","--dispatch-id",restore_unattempted,"--reason",reason)
        return call(binary, root, "dispatch", "review", "--reason", reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("status", "reconcile", "resume"):
        sub.add_parser(name)
    pause = sub.add_parser("pause")
    pause.add_argument("--reason", required=True)
    review = sub.add_parser("review")
    review.add_argument("--reason", required=True)
    review.add_argument("--confirm-desktop-safe", action="store_true", required=True)
    review.add_argument("--restore-unattempted", type=int)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--seconds", type=float, default=3600)
    args = parser.parse_args(argv)
    from . import conversations
    try:
        settings = conversations.config(args.config)
        root = root_from_settings(settings)
        binary = settings["wx_cli"]
        if args.action == "run":
            stopping = []
            def stop_after_current(*_):
                stopping.append(True)
            signal.signal(signal.SIGTERM, stop_after_current)
            signal.signal(signal.SIGINT, stop_after_current)
            stop = time.monotonic() + args.seconds
            next_reconcile=0
            while True:
                if time.monotonic()>=next_reconcile:
                    reconcile(binary,root)
                    next_reconcile=time.monotonic()+10
                value = drain(binary, root, max_items=1)
                if stopping or args.once or time.monotonic() >= stop:
                    break
                time.sleep(1)
        elif args.action == "reconcile":
            value = reconcile(binary,root)
        elif args.action == "pause":
            value = call(binary, root, "dispatch", "pause", "--reason", args.reason)
        elif args.action == "resume":
            # Merely clearing a pause never settles an uncertain request.
            value = call(binary, root, "dispatch", "resume")
        elif args.action == "review":
            value = review_surface(binary, root, args.reason, args.confirm_desktop_safe, args.restore_unattempted)
        else:
            value = call(binary, root, "dispatch", "status")
        print(json.dumps(value, separators=(",", ":")))
        return 0
    except (DispatchError, conversations.ConversationError, ctl.ControlError,
            runner.RunnerError, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
