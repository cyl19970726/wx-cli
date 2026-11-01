"""Collect persisted events and route notifications to explicit managed Sessions."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time

from . import channel_agent as runner
from . import dispatch
from . import wechat_agent_ctl as ctl


def collect(binary, root, settle_seconds=0):
    ids = dispatch.call(binary, root, "status")["conversations"]

    def one(cid):
        started=time.monotonic()
        try:
            binding = dispatch.call(binary, root, "status", "--conversation-id", cid)
            command=[str(binary), "channel", "--state-dir", binding["channel_state_dir"], "sync"]
            synced=runner.one_json(runner.command(command, timeout=90))
            stop=time.monotonic()+5
            while settle_seconds and synced.get("events_appended",0)>0 and time.monotonic()<stop:
                time.sleep(min(settle_seconds,max(0,stop-time.monotonic())))
                synced=runner.one_json(runner.command(command,timeout=90))
            result=dispatch.call(binary, root, "inbox", "reconcile", "--conversation-id", cid)
            result["read_seconds"]=round(time.monotonic()-started,3)
            return result
        except (runner.RunnerError, OSError, subprocess.SubprocessError):
            return {"conversation_id": cid, "error": "read_unavailable"}

    # Bound encrypted snapshot work; the source files are never opened as SQLite.
    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(one, ids))


class PushLoop:
    """Keep one transport alive while managed turns execute; never forward output."""
    def __init__(self, binary, root, config_path, sessions, codex="codex"):
        from .session_adapter import AppServer, ManagedSession, RustDeliveryEvidence, SessionAdapter
        self.binary, self.root, self.config_path = binary, root, config_path
        self.server = AppServer(codex)
        self.adapters = {}
        self.active = {}
        self.errors = {}
        self.uncertain = set()
        try:
            for owner, value in sessions.items():
                session = ManagedSession(**value)
                if session.host != socket.gethostname():
                    raise dispatch.DispatchError("Session belongs to a different execution host")
                self.adapters[owner] = SessionAdapter(session, self.server, RustDeliveryEvidence(binary, root))
        except Exception:
            self.close()
            raise

    def close(self):
        for adapter in self.adapters.values():
            adapter.close()
        self.server.close()

    def tick(self, allow_new=True):
        from .session_adapter import Delivery, DeliveryUnknown, SessionBusy, SessionError
        # Reconcile a previous process's recorded turn before starting any new
        # turn. An unknown reservation without a turn ID stays for inspection.
        for cid in dispatch.call(self.binary, self.root, "status")["conversations"]:
            binding = dispatch.call(self.binary, self.root, "status", "--conversation-id", cid)
            deliveries = dispatch.call(self.binary, self.root, "inbox", "status", "--conversation-id", cid)["deliveries"]
            for previous in deliveries:
                if previous.get("turn_state") not in ("reserved", "recorded"):
                    continue
                for session_owner, adapter in self.adapters.items():
                    if previous.get("session_ref") != adapter.session.session_ref or session_owner in self.active:
                        continue
                    if not previous.get("turn_id"):
                        self.errors[session_owner] = "reserved delivery has unknown turn; inspect original thread"
                        self.uncertain.add(session_owner)
                        continue
                    try:
                        # Another adapter process's live thread lock wins.
                        if adapter.status()["state"] == "busy":
                            continue
                        old = dispatch.call(self.binary, self.root, "inbox", "show",
                                            "--delivery-id", previous["delivery_id"], "--owner", binding["owner"])
                        token = old.get("transport_token")
                        if not token or previous.get("thread_id") != adapter.session.thread_id:
                            raise SessionError("recorded transport identity is incomplete")
                        self.active[session_owner] = (dict(old, token=token), {"turn_id": previous["turn_id"]})
                    except (SessionError, runner.RunnerError, OSError):
                        self.errors[session_owner] = "previous turn requires inspection"
        # Read transport completion independently from explicit business results.
        for owner, value in list(self.active.items()):
            claim, turn = value
            adapter = self.adapters[owner]
            try:
                status = adapter.turn_status(turn["turn_id"])["status"]
                if status in ("completed", "interrupted", "failed"):
                    dispatch.call(self.binary, self.root, "inbox", "finish-turn",
                                  "--delivery-id", claim["delivery_id"], "--token", claim["token"],
                                  "--status", status)
                    del self.active[owner]
            except (SessionError, runner.RunnerError, OSError):
                self.errors[owner] = "turn status unknown; original reservation retained"
        for cid in (dispatch.call(self.binary, self.root, "status")["conversations"] if allow_new else []):
            binding = dispatch.call(self.binary, self.root, "status", "--conversation-id", cid)
            owner = binding["owner"]
            if binding["input"] != "push" or binding["control"] != "active" or owner not in self.adapters or owner in self.active or owner in self.uncertain:
                continue
            adapter = self.adapters[owner]
            if binding.get("owner_session_ref") not in (None,adapter.session.session_ref):
                self.errors[owner]="configured Session differs from the persistent owner binding"
                continue
            claimed = False
            try:
                if adapter.status()["state"] != "idle":
                    continue
                claim = dispatch.call(self.binary, self.root, "inbox", "claim", "--conversation-id", cid,
                                      "--owner", owner, "--lease-seconds", 3600)
                if claim.get("status") != "claimed":
                    continue
                claimed = True
                hint = ("Use wx-agent inbox --config " + shlex.quote(str(self.config_path))
                        + " show --delivery-id " + str(claim["delivery_id"]) + " --owner " + shlex.quote(owner)
                        + " to retrieve your existing lease. Read only this conversation with explicit ID " + cid
                        + "; use inbox events --delivery-id " + str(claim["delivery_id"]) + " --owner " + shlex.quote(owner) + " with the same --config to read its frozen range without source access"
                        + ". Record skip/defer/draft/reply explicitly with inbox complete. "
                        + "For a managed send use --owner " + shlex.quote(owner) + " --expected-epoch " + str(claim["epoch"]) + ". "
                        + "Use --no-wait for send/send-image so the independent desktop executor handles the effect. "
                        + "Before sending inspect existing outbox intents; keep stable IDs delivery-" + str(claim["delivery_id"]) + "-part-N and never replace an attempted or unknown ID. "
                        + "Only an explicit conversations send/send-image intent reaches WeChat; final text is not forwarded.")
                delivery = Delivery(delivery_id=claim["delivery_id"], conversation_id=cid,
                                    cursor_start=claim["cursor_start"], cursor_end=claim["cursor_end"],
                                    owner=owner, owner_epoch=claim["epoch"], claim_token=claim["token"],
                                    attachment_refs=tuple(claim.get("attachment_refs", [])), command_hint=hint)
                turn = adapter.deliver(delivery)
                self.active[owner] = (claim, turn)
                self.errors.pop(owner, None)
            except SessionBusy:
                continue
            except (DeliveryUnknown, SessionError, runner.RunnerError, OSError):
                self.errors[owner] = ("delivery requires inspection; no replacement turn started" if claimed
                                      else "Session unavailable; inbox retained for reconnect")
                if claimed:
                    self.uncertain.add(owner)
        return {"active_sessions": len(self.active), "attention": dict(self.errors)}


def provision(path, owner, cwd, codex, writable_roots=()):
    from .session_adapter import AppServer, SessionAdapter
    existing = ctl.read_json(path) if path.exists() else {"version": 1, "sessions": {}}
    if owner in existing["sessions"]:
        raise dispatch.DispatchError("owner already has a Session; refusing replacement")
    with AppServer(codex) as server:
        session = SessionAdapter.create_managed(server, socket.gethostname(), str(cwd.resolve()),
                                               str(path.parent / ("session-" + owner + ".lock")),
                                               writable_roots=tuple(str(root.resolve()) for root in writable_roots))
    existing["sessions"][owner] = dict(session.__dict__)
    ctl.atomic_json(path, existing)
    return {"provisioned": True, "owner": owner, "ownership": "managed-app-server"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("status", "reconcile", "claim"):
        p = sub.add_parser(name)
        p.add_argument("--id", dest="conversation_id", required=True)
        if name == "claim":
            p.add_argument("--owner", required=True)
    show = sub.add_parser("show")
    show.add_argument("--delivery-id", type=int, required=True)
    show.add_argument("--owner", required=True)
    events = sub.add_parser("events")
    events.add_argument("--delivery-id", type=int, required=True)
    events.add_argument("--owner", required=True)
    events.add_argument("--after", type=int)
    events.add_argument("--limit", type=int, default=50)
    complete = sub.add_parser("complete")
    complete.add_argument("--delivery-id", type=int, required=True)
    complete.add_argument("--token", required=True)
    complete.add_argument("--decision", choices=("skip", "reply", "defer", "draft"), required=True)
    complete.add_argument("--request-id")
    complete.add_argument("--draft-stdin", action="store_true")
    complete.add_argument("--reason")
    complete.add_argument("--condition")
    complete.add_argument("--due-at", type=int)
    resume = sub.add_parser("resume")
    resume.add_argument("--delivery-id", type=int, required=True)
    resume.add_argument("--owner", required=True)
    resume.add_argument("--expected-epoch", type=int, required=True)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--delivery-id", type=int, required=True)
    cancel.add_argument("--owner", required=True)
    cancel.add_argument("--expected-epoch", type=int, required=True)
    cancel.add_argument("--reason", required=True)
    run = sub.add_parser("run")
    run.add_argument("--session-config", type=Path)
    run.add_argument("--codex", default="codex")
    run.add_argument("--once", action="store_true")
    run.add_argument("--seconds", type=float, default=3600)
    run.add_argument("--interval", type=float, default=2)
    init = sub.add_parser("provision")
    init.add_argument("--session-config", type=Path, required=True)
    init.add_argument("--owner", required=True)
    init.add_argument("--cwd", type=Path, required=True)
    init.add_argument("--codex", default="codex")
    init.add_argument("--writable-root", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    from . import conversations
    push = None
    executor = None
    executor_log = None
    runner_lock = None
    runner_lock_held = False
    collector = None
    try:
        settings = conversations.config(args.config)
        binary, root = settings["wx_cli"], dispatch.root_from_settings(settings)
        if args.action == "provision":
            value = provision(args.session_config, args.owner, args.cwd, args.codex, args.writable_root)
        elif args.action == "events":
            claim=dispatch.call(binary,root,"inbox","show","--delivery-id",args.delivery_id,"--owner",args.owner)
            start=claim["cursor_start"]-1 if args.after is None else args.after
            if not claim["cursor_start"]-1<=start<=claim["cursor_end"] or not 1<=args.limit<=100:
                raise dispatch.DispatchError("event pagination must stay within the frozen delivery range")
            raw=runner.command([str(binary),"channel","--state-dir",claim["channel_state_dir"],"events",
                                "--after",str(start),"--limit",str(args.limit)],timeout=30)
            page=[json.loads(line) for line in raw.splitlines() if line.strip()]
            page=[event for event in page if event["cursor"]<=claim["cursor_end"]]
            cursor=page[-1]["cursor"] if page else start
            value={"delivery_id":args.delivery_id,"conversation_id":claim["conversation_id"],"events":page,
                   "next_cursor":cursor,"has_more":cursor<claim["cursor_end"]}
        elif args.action == "run":
            if args.interval < 1 or not 0 < args.seconds <= 86400:
                raise dispatch.DispatchError("use interval >=1 second and duration 1..86400 seconds")
            runner_lock = dispatch.exclusive(root,"inbox-runner.lock")
            runner_lock.__enter__()
            runner_lock_held = True
            stopping=[]
            def stop_after_current(*_):
                stopping.append(True)
            signal.signal(signal.SIGTERM,stop_after_current)
            signal.signal(signal.SIGINT,stop_after_current)
            fd=os.open(root/"executor.log",os.O_CREAT|os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW,0o600)
            ctl.private(root/"executor.log")
            executor_log=os.fdopen(fd,"ab",buffering=0)
            executor=subprocess.Popen([sys.executable,"-m","wx_agent.cli","dispatch","--config",str(args.config),
                                       "run","--seconds",str(args.seconds)],stdout=executor_log,stderr=executor_log)
            if args.session_config:
                sessions = ctl.read_json(args.session_config)
                if sessions.get("version") != 1:
                    raise dispatch.DispatchError("invalid Session configuration")
                push = PushLoop(binary, root, args.config, sessions["sessions"], args.codex)
            stop = time.monotonic() + args.seconds
            collector=ThreadPoolExecutor(max_workers=1)
            collection=collector.submit(collect,binary,root,1)
            last_collection=[]
            collected=False
            notified_once=False
            next_collection=0
            while True:
                if collection is not None and collection.done():
                    try:
                        last_collection=collection.result()
                    except (runner.RunnerError,OSError):
                        last_collection=[{"error":"collection unavailable; durable inbox retained"}]
                    collected=True
                    collection=None
                    next_collection=time.monotonic()+args.interval
                if collection is None and not args.once and time.monotonic()>=next_collection:
                    collection=collector.submit(collect,binary,root,1)
                value = {"collection": last_collection,"collection_active":collection is not None}
                if push:
                    value["push"] = push.tick(allow_new=collected and (not notified_once or not args.once))
                    if collected:
                        notified_once=True
                value["dispatch"] = dispatch.call(binary,root,"dispatch","status")
                value["executor_alive"]=executor.poll() is None
                value["pid"]=os.getpid()
                ctl.atomic_json(root / "inbox-health.json", {"version": 1, "updated_at": time.time(), **value})
                if stopping or (args.once and collected and (not push or not push.active)) or time.monotonic() >= stop:
                    break
                time.sleep(min(args.interval,1))
        else:
            parts = ["inbox", args.action]
            for key in ("conversation_id", "delivery_id", "owner", "token", "decision", "request_id", "expected_epoch", "reason", "condition", "due_at"):
                value = getattr(args, key, None)
                if value is not None:
                    parts += ["--" + key.replace("_", "-"), str(value)]
            if getattr(args, "draft_stdin", False):
                parts += ["--draft-stdin"]
            value = dispatch.call(binary, root, *parts,
                                  data=sys.stdin.read(65537) if getattr(args, "draft_stdin", False) else None)
        print(json.dumps(value, separators=(",", ":")))
        return 0
    except (dispatch.DispatchError, conversations.ConversationError, ctl.ControlError,
            runner.RunnerError, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    finally:
        if executor and executor.poll() is None:
            executor.terminate()
        if push:
            push.close()
        if collector:
            collector.shutdown(wait=True)
        if executor and executor.poll() is None:
            # The child handles SIGTERM at its next safe boundary; never kill
            # an in-flight Rust/GUI attempt to make shutdown appear immediate.
            try:
                executor.wait(timeout=95)
            except subprocess.TimeoutExpired:
                pass
        if executor_log:
            executor_log.close()
        if runner_lock_held:
            runner_lock.__exit__(None,None,None)
