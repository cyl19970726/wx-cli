"""Optional Codex Session orchestration for managed inbox deliveries."""
import re
import shlex
import socket
import time
from pathlib import Path

from .. import channel_agent as runner
from .. import dispatch
from .. import wechat_agent_ctl as ctl
from ..errors import SessionError


class PushLoop:
    """Keep one transport alive while managed turns execute; never forward output."""
    def __init__(self, binary, root, config_path, sessions, codex="codex"):
        from ..session_adapter import AppServer, ManagedSession, RustDeliveryEvidence, SessionAdapter
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
        from ..session_adapter import Delivery, DeliveryUnknown, SessionBusy, SessionError
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
                                      "--owner", owner, "--expected-epoch", binding["epoch"],
                                      "--lease-seconds", 3600)
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
    from ..session_adapter import AppServer, SessionAdapter
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}",owner):
        raise dispatch.DispatchError("owner must be a bounded safe identifier")
    if not path.is_absolute() or path.resolve()!=path or not cwd.is_dir():
        raise dispatch.DispatchError("provisioning requires canonical config and an existing project directory")
    ctl.private(path.parent,True)
    with dispatch.exclusive(path.parent,path.name+".provision.lock",blocking=True):
        existing = ctl.read_json(path) if path.exists() else {"version": 1, "sessions": {}}
        metadata=existing.setdefault("provisioning",{})
        if owner in existing["sessions"] or owner in metadata:
            raise dispatch.DispatchError("owner already has a Session; inspect its saved initialization instead of replacing it")
        with AppServer(codex) as server:
            metadata[owner]={"state":"creating"}
            ctl.atomic_json(path,existing)
            session = SessionAdapter.create_managed(server, socket.gethostname(), str(cwd.resolve()),
                                                   str(path.parent / ("session-" + owner + ".lock")),
                                                   writable_roots=tuple(str(root.resolve()) for root in writable_roots))
            existing["sessions"][owner]=dict(session.__dict__)
            metadata[owner]={"state":"starting","thread_id":session.thread_id}
            # Save the original reference before the first mutating turn RPC.
            ctl.atomic_json(path,existing)
            with dispatch.exclusive(path.parent,Path(session.lock_path).name):
                turn=server.request("turn/start",{
                    "threadId":session.thread_id,"clientUserMessageId":"wx-provision-"+owner,
                    "input":[{"type":"text","text":"Initialize this managed Session with a persistent checkpoint. No WeChat data is present. Do not use tools, read messages, change files or send anything. Acknowledge readiness briefly and wait for scoped notifications."}],
                    "cwd":session.cwd,"approvalPolicy":"never","sandboxPolicy":{"type":"readOnly"}})
                turn_id=turn.get("turn",{}).get("id")
                if not turn_id:
                    raise SessionError("initialization response unknown; original thread reference is retained")
                metadata[owner].update(state="initializing",turn_id=turn_id)
                ctl.atomic_json(path,existing)
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    try:
                        thread=server.request("thread/read",{"threadId":session.thread_id,"includeTurns":True})["thread"]
                    except SessionError as exc:
                        if "rollout" not in str(exc) and "not loaded" not in str(exc):
                            raise
                        time.sleep(.2)
                        continue
                    state=next((item.get("status") for item in thread.get("turns",[]) if item.get("id")==turn_id),None)
                    if state=="completed":
                        break
                    if state in ("interrupted","failed"):
                        raise SessionError("initialization turn did not complete; original reference retained")
                    time.sleep(.2)
                else:
                    raise SessionError("initialization timed out; inspect original thread and turn")
        # A completed model turn is insufficient: cold-resume the same ID.
        with AppServer(codex) as cold:
            adapter=SessionAdapter(session,cold,None)
            if adapter.turn_status(turn_id)["status"]!="completed":
                raise SessionError("cold resume could not verify initialization")
        metadata[owner].update(state="ready",cold_resume_verified=True)
        ctl.atomic_json(path,existing)
        return {"provisioned": True, "owner": owner, "ownership": "managed-app-server",
                "initialized":True,"cold_resume_verified":True}
