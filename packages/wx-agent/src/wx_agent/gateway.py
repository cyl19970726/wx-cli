"""One request per process, private-policy JSON RPC for managed applications."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys

from .application_client import ApplicationClient, ApplicationError, ResultUnknown, APP_ID
from . import channel_agent as runner
from .errors import IntegrationError


MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
CORRELATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
SCOPES = {"send", "result", "inbox"}
METHOD_SCOPE = {"send_text": "send", "result": "result", **dict.fromkeys(
    ("binding", "inbox_status", "inbox_show", "inbox_claim", "transport_status", "reserve_turn",
     "record_turn", "finish_turn", "events", "complete", "complete_observed_reply"), "inbox")}
PARAMS = {
    "send_text": ({"conversation_id", "request_id", "text"}, set()),
    "result": ({"conversation_id", "request_id"}, set()),
    "binding": ({"conversation_id"}, set()),
    "inbox_status": ({"conversation_id"}, set()),
    "inbox_show": ({"conversation_id", "delivery_id"}, set()),
    "inbox_claim": ({"conversation_id"}, {"lease_seconds"}),
    "transport_status": ({"conversation_id"}, set()),
    "reserve_turn": ({"conversation_id", "delivery_id", "token"}, set()),
    "record_turn": ({"conversation_id", "delivery_id", "token", "turn_id"}, set()),
    "finish_turn": ({"conversation_id", "delivery_id", "token", "status"}, set()),
    "events": ({"conversation_id", "delivery_id"}, {"after", "limit"}),
    "complete": ({"conversation_id", "delivery_id", "token", "decision"},
                 {"request_id", "text", "reason", "condition", "due_at"}),
    "complete_observed_reply": ({"conversation_id", "delivery_id", "token", "request_id", "reason"}, set()),
}


class GatewayError(IntegrationError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _private_json(path: Path) -> dict:
    if not path.is_absolute() or path.resolve() != path:
        raise GatewayError("invalid_config")
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise GatewayError("invalid_config")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise GatewayError("invalid_config") from exc
    if not isinstance(value, dict):
        raise GatewayError("invalid_config")
    return value


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise GatewayError("invalid_params")


def _identity(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _positive(value):
    return type(value) is int and value > 0


def _safe(value):
    """Never expose internal filesystem routes in coordinator results."""
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()
                if key not in {"channel_state_dir", "state_dir", "source", "source_path", "file_path", "path"}}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


class Gateway:
    def __init__(self, policy_path: Path):
        policy = _private_json(Path(policy_path))
        if set(policy) != {"version", "applications"} or policy["version"] != 1 or not isinstance(policy["applications"], dict):
            raise GatewayError("invalid_config")
        self.applications = policy["applications"]
        for app_id, grant in self.applications.items():
            if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id) or not isinstance(grant, dict):
                raise GatewayError("invalid_config")
            if set(grant) != {"token_sha256", "client_config", "permissions", "session_bindings"}:
                raise GatewayError("invalid_config")
            digest = grant["token_sha256"]
            path = grant["client_config"]
            permissions = grant["permissions"]
            bindings = grant["session_bindings"]
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not isinstance(path, str) or not Path(path).is_absolute() or Path(path).resolve() != Path(path)
                    or not isinstance(permissions, list) or not permissions
                    or not all(isinstance(item, str) for item in permissions)
                    or len(set(permissions)) != len(permissions)
                    or not set(permissions) <= SCOPES or not isinstance(bindings, dict)
                    or ("inbox" not in permissions and bindings)):
                raise GatewayError("invalid_config")
            for cid, pin in bindings.items():
                if (not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{32}", cid)
                        or not isinstance(pin, dict) or set(pin) != {"session_ref", "thread_id"}
                        or not all(_identity(pin[key]) for key in ("session_ref", "thread_id"))):
                    raise GatewayError("invalid_config")

    @staticmethod
    def _co(client, *parts, data=None):
        argv = [str(client.wx_cli), "coordinator", "--state-dir", str(client.coordinator_root), *map(str, parts)]
        try:
            return runner.one_json(runner.command(argv, data=data, timeout=30, limit=MAX_RESPONSE))
        except (runner.RunnerError, OSError, ValueError) as exc:
            raise GatewayError("result_unknown" if parts[0] == "inbox" and parts[1] in
                               {"claim", "reserve-turn", "record-turn", "finish-turn", "complete"}
                               else "backend_unavailable") from exc

    @staticmethod
    def _channel(client, state, after, limit):
        argv = [str(client.wx_cli), "channel", "--state-dir", str(state), "events",
                "--after", str(after), "--limit", str(limit)]
        try:
            raw = runner.command(argv, timeout=30, limit=MAX_RESPONSE)
            return [json.loads(line) for line in raw.splitlines() if line.strip()]
        except (runner.RunnerError, OSError, ValueError, TypeError) as exc:
            raise GatewayError("backend_unavailable") from exc

    def _route(self, client, cid):
        entry = client.conversations[cid]
        try:
            marker = _private_json(entry["state_dir"] / "managed-coordinator.json")
        except GatewayError as exc:
            raise GatewayError("binding_changed") from exc
        if marker.get("version") != 1 or marker.get("root") != str(client.coordinator_root):
            raise GatewayError("binding_changed")
        binding = self._co(client, "status", "--conversation-id", cid)
        if (binding.get("conversation_id") != cid or binding.get("channel_state_dir") != str(entry["state_dir"])
                or not isinstance(binding.get("owner"), str) or type(binding.get("epoch")) is not int):
            raise GatewayError("binding_changed")
        return binding

    def _binding(self, client, cid, pin):
        entry = client.conversations[cid]
        binding = self._route(client, cid)
        if (binding["owner"] != entry["owner"] or binding["epoch"] != entry["expected_epoch"]
                or binding.get("control") != "active"
                or binding.get("owner_session_ref") not in (None, pin["session_ref"])):
            raise GatewayError("binding_changed")
        return binding

    def _show(self, client, cid, pin, binding, delivery_id):
        if not _positive(delivery_id):
            raise GatewayError("invalid_params")
        shown = self._co(client, "inbox", "show", "--delivery-id", delivery_id,
                         "--owner", binding["owner"])
        if (shown.get("delivery_id") != delivery_id or shown.get("conversation_id") != cid
                or shown.get("owner") != binding["owner"]
                or shown.get("epoch") != binding["epoch"]
                or shown.get("channel_state_dir") != binding["channel_state_dir"]
                or (shown.get("epoch") == binding["epoch"]
                    and binding.get("owner_session_ref") not in (None, pin["session_ref"]))
                or shown.get("session_ref") not in (None, pin["session_ref"])
                or shown.get("thread_id") not in (None, pin["thread_id"])):
            raise GatewayError("delivery_out_of_scope")
        return shown

    def _transport_show(self, client, cid, pin, binding, delivery_id, grant_epoch):
        if not _positive(delivery_id):
            raise GatewayError("invalid_params")
        shown = self._co(client, "inbox", "show", "--delivery-id", delivery_id,
                         "--owner", binding["owner"])
        if (shown.get("delivery_id") != delivery_id or shown.get("conversation_id") != cid
                or shown.get("channel_state_dir") != binding["channel_state_dir"]
                or shown.get("owner") != binding["owner"]
                or type(shown.get("epoch")) is not int or not 0 < shown["epoch"] <= grant_epoch
                or shown.get("session_ref") != pin["session_ref"]
                or shown.get("thread_id") not in (None, pin["thread_id"])
                or (shown.get("turn_state") == "recorded"
                    and (shown.get("thread_id") != pin["thread_id"] or not shown.get("turn_id")))
                or shown.get("turn_state") not in ("reserved", "recorded", "completed", "interrupted", "failed")
                or not isinstance(shown.get("transport_token"), str) or not shown["transport_token"]):
            raise GatewayError("delivery_out_of_scope")
        return shown

    def request(self, envelope: dict) -> dict:
        _fields(envelope, {"protocol_version", "id", "application_id", "token", "method", "params"})
        if envelope["protocol_version"] != 1 or not isinstance(envelope["id"], str) or not CORRELATION.fullmatch(envelope["id"]):
            raise GatewayError("invalid_request")
        app_id, token, method, params = (envelope[key] for key in ("application_id", "token", "method", "params"))
        grant = self.applications.get(app_id) if isinstance(app_id, str) else None
        if not isinstance(token, str) or len(token) > 512 or not hmac.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(), grant["token_sha256"] if grant else "0" * 64):
            raise GatewayError("auth_failed")
        if not isinstance(method, str) or method not in METHOD_SCOPE:
            raise GatewayError("method_denied")
        scope = METHOD_SCOPE[method]
        if scope not in grant["permissions"]:
            raise GatewayError("permission_denied")
        if not isinstance(params, dict) or not isinstance(params.get("conversation_id"), str):
            raise GatewayError("invalid_params")
        _fields(params, *PARAMS[method])
        cid = params["conversation_id"]
        try:
            client = ApplicationClient(Path(grant["client_config"]))
        except (ApplicationError, OSError) as exc:
            raise GatewayError("invalid_config") from exc
        if client.application_id != app_id or cid not in client.conversations:
            raise GatewayError("conversation_out_of_scope")
        if (method == "complete_observed_reply" or method == "complete" and params.get("decision") == "reply") and "send" not in grant["permissions"]:
            raise GatewayError("permission_denied")
        if method == "send_text":
            _fields(params, {"conversation_id", "request_id", "text"})
            return client.send_text(cid, params["request_id"], params["text"])
        if method == "result":
            _fields(params, {"conversation_id", "request_id"})
            return client.status(cid, params["request_id"])
        pin = grant["session_bindings"].get(cid)
        if pin is None:
            raise GatewayError("conversation_out_of_scope")
        if method in ("transport_status", "record_turn", "finish_turn"):
            binding = self._route(client, cid)
            grant_epoch = client.conversations[cid]["expected_epoch"]
            if grant_epoch > binding["epoch"]:
                raise GatewayError("binding_changed")
            if method == "transport_status":
                value = self._co(client, "inbox", "status", "--conversation-id", cid)
                if value.get("conversation_id") != cid or not isinstance(value.get("deliveries"), list):
                    raise GatewayError("backend_unavailable")
                candidates = [row for row in value["deliveries"] if isinstance(row, dict)
                              and row.get("turn_state") in ("reserved", "recorded")
                              and type(row.get("epoch")) is int and 0 < row["epoch"] <= grant_epoch
                              and row.get("session_ref") == pin["session_ref"]
                              and row.get("thread_id") in (None, pin["thread_id"])
                              and (row["turn_state"] != "recorded"
                                   or (row.get("thread_id") == pin["thread_id"] and row.get("turn_id")))]
                if len(candidates) > 16:
                    raise GatewayError("backend_unavailable")
                rows = []
                for row in candidates:
                    shown = self._transport_show(client, cid, pin, binding, row.get("delivery_id"), grant_epoch)
                    if shown["turn_state"] in ("reserved", "recorded"):
                        rows.append({key: shown.get(key) for key in ("delivery_id", "conversation_id", "owner", "epoch",
                                    "status", "turn_state", "session_ref", "thread_id", "turn_id", "transport_token")})
                return {"conversation_id": cid, "deliveries": rows}
            shown = self._transport_show(client, cid, pin, binding, params["delivery_id"], grant_epoch)
            if not isinstance(params["token"], str) or params["token"] != shown["transport_token"]:
                raise GatewayError("delivery_out_of_scope")
            if method == "record_turn":
                if shown["turn_state"] not in ("reserved", "recorded") or not _identity(params["turn_id"]):
                    raise GatewayError("invalid_params")
                return self._co(client, "inbox", "record-turn", "--delivery-id", shown["delivery_id"],
                                "--token", params["token"], "--thread-id", pin["thread_id"],
                                "--turn-id", params["turn_id"])
            if params["status"] not in ("completed", "interrupted", "failed"):
                raise GatewayError("invalid_params")
            if params["status"] == "completed" and shown["turn_state"] == "reserved":
                raise GatewayError("invalid_params")
            return self._co(client, "inbox", "finish-turn", "--delivery-id", shown["delivery_id"],
                            "--token", params["token"], "--status", params["status"])
        binding = self._binding(client, cid, pin)
        if method == "binding":
            _fields(params, {"conversation_id"})
            return {key: binding.get(key) for key in ("conversation_id", "owner", "epoch", "input",
                    "reply_policy", "control", "owner_session_ref", "watermark", "processed_cursor")}
        if method == "inbox_status":
            _fields(params, {"conversation_id"})
            value = self._co(client, "inbox", "status", "--conversation-id", cid)
            if value.get("conversation_id") != cid or value.get("owner") != binding["owner"] or value.get("epoch") != binding["epoch"]:
                raise GatewayError("binding_changed")
            return _safe(value)
        if method == "inbox_claim":
            _fields(params, {"conversation_id"}, {"lease_seconds"})
            lease = params.get("lease_seconds", 300)
            if type(lease) is not int or not 30 <= lease <= 3600:
                raise GatewayError("invalid_params")
            value = self._co(client, "inbox", "claim", "--conversation-id", cid, "--owner", binding["owner"],
                             "--expected-epoch", binding["epoch"], "--lease-seconds", lease,
                             "--session-ref", pin["session_ref"])
            if value.get("status") == "claimed" and (value.get("conversation_id") != cid
                    or value.get("owner") != binding["owner"] or value.get("epoch") != binding["epoch"]):
                raise GatewayError("result_unknown")
            return _safe(value)
        shown = self._show(client, cid, pin, binding, params["delivery_id"])
        if method == "inbox_show":
            _fields(params, {"conversation_id", "delivery_id"})
            return _safe(shown)
        if method == "events":
            _fields(params, {"conversation_id", "delivery_id"}, {"after", "limit"})
            after = params.get("after", shown["cursor_start"] - 1)
            limit = params.get("limit", 50)
            if type(after) is not int or not shown["cursor_start"] - 1 <= after <= shown["cursor_end"] or type(limit) is not int or not 1 <= limit <= 100:
                raise GatewayError("invalid_params")
            events = self._channel(client, shown["channel_state_dir"], after, limit)
            events = [event for event in events if event.get("cursor", 0) <= shown["cursor_end"]]
            cursor = events[-1]["cursor"] if events else after
            return {"conversation_id": cid, "delivery_id": shown["delivery_id"],
                    "events": _safe(events), "next_cursor": cursor, "has_more": cursor < shown["cursor_end"]}
        if method in ("reserve_turn", "complete", "complete_observed_reply"):
            token = params.get("token")
            if not isinstance(token, str) or not token or token != shown.get("token"):
                raise GatewayError("delivery_out_of_scope")
        if method == "reserve_turn":
            _fields(params, {"conversation_id", "delivery_id", "token"})
            return self._co(client, "inbox", "reserve-turn", "--delivery-id", shown["delivery_id"],
                            "--token", token, "--session-ref", pin["session_ref"])
        if method == "complete_observed_reply":
            key = "reply-" + cid + "-" + str(shown["delivery_id"])
            reason = params["reason"]
            if (params["request_id"] != key or binding.get("reply_policy") != "authorized"
                    or shown.get("turn_state") != "recorded" or shown.get("thread_id") != pin["thread_id"]
                    or not shown.get("turn_id") or not isinstance(reason, str)
                    or not reason.strip() or len(reason.encode()) > 512 or "\0" in reason):
                raise GatewayError("invalid_params")
            existing = client.status(cid, key)
            if (existing.get("status") not in ("ui_observed", "recipient_confirmed")
                    or existing.get("attempt_number") != 1
                    or existing.get("request_id") != client.application_id + "." + key):
                raise GatewayError("reply_not_observed")
            return self._co(client, "inbox", "complete-observed-reply", "--delivery-id", shown["delivery_id"],
                            "--token", token, "--publisher", client.application_id,
                            "--request-id", existing["request_id"], "--reason", reason)
        if method == "complete":
            _fields(params, {"conversation_id", "delivery_id", "token", "decision"},
                    {"request_id", "text", "reason", "condition", "due_at"})
            decision = params["decision"]
            if decision not in ("skip", "defer", "draft", "reply") or ("text" in params) != (decision == "draft") or ("request_id" in params) != (decision == "reply"):
                raise GatewayError("invalid_params")
            args = ["inbox", "complete", "--delivery-id", shown["delivery_id"], "--token", token,
                    "--decision", decision]
            if decision == "reply":
                if "send" not in grant["permissions"]:
                    raise GatewayError("permission_denied")
                existing = client.status(cid, params["request_id"])
                args += ["--request-id", existing["request_id"]]
            if decision == "draft":
                if not isinstance(params["text"], str) or not params["text"].strip() or len(params["text"].encode()) > 65536:
                    raise GatewayError("invalid_params")
                args += ["--draft-stdin"]
            for key in ("reason", "condition", "due_at"):
                if key in params:
                    value = params[key]
                    if (key == "due_at" and (type(value) is not int or value < 0)) or (key != "due_at" and (not isinstance(value, str) or not value.strip() or len(value) > 512)):
                        raise GatewayError("invalid_params")
                    args += ["--" + key.replace("_", "-"), value]
            return self._co(client, *args, data=params.get("text") if decision == "draft" else None)
        raise GatewayError("method_denied")

    def handle(self, envelope) -> dict:
        request_id = envelope.get("id") if isinstance(envelope, dict) else None
        if not isinstance(request_id, str) or not CORRELATION.fullmatch(request_id):
            request_id = None
        try:
            result = self.request(envelope)
            response = {"protocol_version": 1, "id": request_id, "ok": True, "result": result}
        except GatewayError as exc:
            response = {"protocol_version": 1, "id": request_id, "ok": False, "error": {"code": exc.code}}
        except ResultUnknown:
            response = {"protocol_version": 1, "id": request_id, "ok": False, "error": {"code": "result_unknown"}}
        except ApplicationError as exc:
            code = exc.code if exc.code in {"conversation_out_of_scope", "invalid_request_id", "already_namespaced",
                    "invalid_text", "owner_epoch_drift", "request_scope_drift", "text_conflict"} else "operation_failed"
            response = {"protocol_version": 1, "id": request_id, "ok": False, "error": {"code": code}}
        except Exception:
            response = {"protocol_version": 1, "id": request_id, "ok": False,
                        "error": {"code": "operation_failed"}}
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_RESPONSE:
            return {"protocol_version": 1, "id": request_id, "ok": False, "error": {"code": "response_too_large"}}
        return response


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("action", choices=("request",))
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise GatewayError("request_too_large")
        envelope = json.loads(raw.decode("utf-8"))
        result = Gateway(args.config).handle(envelope)
    except GatewayError as exc:
        result = {"protocol_version": 1, "id": None, "ok": False,
                  "error": {"code": exc.code}}
    except (ValueError, UnicodeError, OSError):
        result = {"protocol_version": 1, "id": None, "ok": False,
                  "error": {"code": "invalid_request"}}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
