"""Session identity, scoped inbox routing and reservations for queue delivery.

This module has no native event callbacks or model/desktop lifecycle operations.
Rust remains authoritative for every lease, transport and business decision.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from . import channel_agent as runner
from . import conversations, dispatch, wechat_agent_ctl as ctl


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}\Z")


class PluginError(RuntimeError):
    """A bounded routing error that never exposes credentials or message bodies."""


def canonical(value, *, directory=False, executable=False):
    if not isinstance(value, str):
        raise PluginError("invalid_path")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise PluginError("invalid_path")
    if directory and not path.is_dir() or executable and not (path.is_file() and os.access(path, os.X_OK)):
        raise PluginError("path_unavailable")
    return path


@dataclass(frozen=True)
class Route:
    session_id: str
    conversation_id: str
    owner: str
    expected_epoch: int
    project: Path
    agent_host: str

    @property
    def session_ref(self):
        return "codex:" + self.agent_host + ":" + self.session_id


class LocalInbox:
    def __init__(self, config_path):
        self.config_path = config_path
        settings = conversations.config(config_path)
        self.binary = settings["wx_cli"]
        self.root = dispatch.root_from_settings(settings)

    def call(self, *parts):
        return runner.one_json(runner.command(
            [str(self.binary), "coordinator", "--state-dir", str(self.root), *map(str, parts)], timeout=15))

    def transport_records(self, route):
        current = self.call("status", "--conversation-id", route.conversation_id)
        if current.get("conversation_id") != route.conversation_id:
            raise PluginError("binding_changed")
        records = []
        for item in self.call("inbox", "status", "--conversation-id", route.conversation_id)["deliveries"]:
            if item.get("turn_state") not in ("reserved", "recorded") or item.get("session_ref") != route.session_ref:
                continue
            old = self.call("inbox", "show", "--delivery-id", item["delivery_id"], "--owner", current["owner"])
            if (old.get("conversation_id") == route.conversation_id and old.get("epoch", route.expected_epoch + 1) <= route.expected_epoch
                    and old.get("session_ref") == route.session_ref and old.get("thread_id") in (None, route.session_id)):
                records.append(old)
        return records


class GatewayInbox:
    """Map the plugin's fixed inbox operations to the scoped remote API."""
    def __init__(self, client, routes):
        self.client = client
        self.routes = {route.conversation_id: route for route in routes}
        self.delivery_routes = {}

    def remember(self, cid, result):
        if result.get("conversation_id", cid) != cid:
            raise PluginError("gateway_scope_changed")
        for item in result.get("deliveries", [result]):
            if item.get("conversation_id", cid) != cid:
                raise PluginError("gateway_scope_changed")
            delivery_id = item.get("delivery_id")
            if delivery_id is not None:
                if type(delivery_id) is not int or delivery_id <= 0:
                    raise PluginError("gateway_delivery_changed")
                previous = self.delivery_routes.get(delivery_id, cid)
                if previous != cid:
                    raise PluginError("gateway_scope_changed")
                self.delivery_routes[delivery_id] = cid
        return result

    def transport_records(self, route):
        value = self.remember(route.conversation_id, self.client.request("transport_status", {"conversation_id": route.conversation_id}))
        return value["deliveries"]

    def call(self, *parts):
        if parts[0] == "status":
            method, flags = "binding", parts[1:]
        elif len(parts) >= 2 and parts[0] == "inbox":
            action, flags = parts[1], parts[2:]
            method = {"status": "inbox_status", "show": "inbox_show", "claim": "inbox_claim",
                      "reserve-turn": "reserve_turn", "record-turn": "record_turn", "finish-turn": "finish_turn"}.get(action)
        else:
            raise PluginError("unsupported_gateway_operation")
        if not method or len(flags) % 2:
            raise PluginError("unsupported_gateway_operation")
        values = {}
        allowed = {"conversation_id", "delivery_id", "owner", "expected_epoch", "lease_seconds", "token", "session_ref", "thread_id", "turn_id", "status"}
        for i in range(0, len(flags), 2):
            flag = str(flags[i])
            if not flag.startswith("--"):
                raise PluginError("unsupported_gateway_operation")
            key = flag[2:].replace("-", "_")
            if key not in allowed or key in values:
                raise PluginError("unsupported_gateway_operation")
            values[key] = flags[i + 1]
        cid = values.get("conversation_id") or self.delivery_routes.get(values.get("delivery_id"))
        if cid not in self.routes:
            raise PluginError("gateway_scope_changed")
        route = self.routes[cid]
        for key, expected in (("owner", route.owner), ("expected_epoch", route.expected_epoch),
                              ("session_ref", route.session_ref), ("thread_id", route.session_id)):
            if key in values and values[key] != expected:
                raise PluginError("gateway_binding_changed")
            values.pop(key, None)
        values["conversation_id"] = cid
        return self.remember(cid, self.client.request(method, values))


class SessionBindings:
    def __init__(self, config_path, *, inbox=None, allow_shared_routes=False):
        self.config_path = canonical(str(config_path))
        value = ctl.read_json(self.config_path)
        required = {"version", "conversation_config", "wx_agent", "codex", "bindings"}
        if any(key in value for key in ("remote", "remote_auth_token_env", "resume_queued")):
            raise PluginError("hook_transport_removed")
        if not required <= set(value) or set(value) - required - {"gateway_config", "gateway_client", "allow_shared_session"} or value["version"] != 1:
            raise PluginError("invalid_config")
        self.channel_config = canonical(value["conversation_config"])
        self.wx_agent = canonical(value["wx_agent"], executable=True)
        self.codex = canonical(value["codex"], executable=True)
        shared = value.get("allow_shared_session", False)
        if type(shared) is not bool or (shared and not allow_shared_routes):
            raise PluginError("shared_session_requires_queue_transport")
        self.routes = []
        seen_sessions, seen_conversations = set(), set()
        if not isinstance(value["bindings"], list) or not value["bindings"]:
            raise PluginError("invalid_bindings")
        for item in value["bindings"]:
            if not isinstance(item, dict) or set(item) != {"session_id", "conversation_id", "owner", "expected_epoch", "project", "agent_host"}:
                raise PluginError("invalid_binding")
            if any(not isinstance(item[key], str) or not SAFE_ID.fullmatch(item[key]) for key in ("session_id", "owner", "agent_host")):
                raise PluginError("invalid_binding")
            if not isinstance(item["conversation_id"], str) or not conversations.CONVERSATION_ID.fullmatch(item["conversation_id"]):
                raise PluginError("invalid_binding")
            if type(item["expected_epoch"]) is not int or item["expected_epoch"] < 0:
                raise PluginError("invalid_binding")
            if (not shared and item["session_id"] in seen_sessions) or item["conversation_id"] in seen_conversations:
                raise PluginError("ambiguous_binding")
            seen_sessions.add(item["session_id"]); seen_conversations.add(item["conversation_id"])
            self.routes.append(Route(**dict(item, project=canonical(item["project"], directory=True))))
        if shared:
            for route in self.routes:
                peers = [r for r in self.routes if r.session_id == route.session_id]
                if any(r.project != route.project or r.agent_host != route.agent_host for r in peers):
                    raise PluginError("shared_session_identity_changed")
        self.gateway_config = None
        self.gateway_client = None
        if ("gateway_config" in value) != ("gateway_client" in value):
            raise PluginError("invalid_gateway_config")
        if "gateway_config" in value:
            from .gateway_client import GatewayClient
            self.gateway_config = canonical(value["gateway_config"])
            self.gateway_client = canonical(value["gateway_client"], executable=True)
            selected = GatewayInbox(GatewayClient(self.gateway_config), self.routes)
        else:
            selected = LocalInbox(self.channel_config) if inbox is None else inbox
        self.inbox = inbox or selected

    def binding(self, route):
        value = self.inbox.call("status", "--conversation-id", route.conversation_id)
        if (value.get("conversation_id") != route.conversation_id or value.get("owner") != route.owner
                or value.get("epoch") != route.expected_epoch or value.get("control") != "active"
                or value.get("input") != "push" or value.get("owner_session_ref") not in (None, route.session_ref)):
            raise PluginError("binding_changed")
        return value

    def deliveries(self, route):
        self.binding(route)
        return self.inbox.call("inbox", "status", "--conversation-id", route.conversation_id)["deliveries"]

    def show(self, route, delivery_id, *, prior_transport=False):
        value = self.inbox.call("inbox", "show", "--delivery-id", delivery_id, "--owner", route.owner)
        if (value.get("conversation_id") != route.conversation_id or value.get("delivery_id") != delivery_id
                or (not prior_transport and (value.get("owner") != route.owner or value.get("epoch") != route.expected_epoch))):
            raise PluginError("delivery_changed")
        return value

    def reserve(self, route, claim):
        if (claim.get("conversation_id") != route.conversation_id or claim.get("owner") != route.owner
                or claim.get("epoch") != route.expected_epoch or not claim.get("token")):
            raise PluginError("claim_changed")
        result = self.inbox.call("inbox", "reserve-turn", "--delivery-id", claim["delivery_id"],
                                 "--token", claim["token"], "--session-ref", route.session_ref)
        if result.get("delivery_id") != claim["delivery_id"] or result.get("turn_state") != "reserved":
            raise PluginError("reservation_unknown")
        return result.get("already_reserved") is False

    @staticmethod
    def notification(route, delivery_id, transport_token):
        if not isinstance(transport_token, str) or not transport_token:
            raise PluginError("notification_identity_unavailable")
        identity = "wx-cli-native-notice-v1\0" + route.session_ref + "\0" + str(delivery_id) + "\0" + transport_token
        return "WXCLI_DELIVERY_V1 " + json.dumps({"conversation_id": route.conversation_id,
                                                  "delivery_id": delivery_id,
                                                  "notification_ref": hashlib.sha256(identity.encode()).hexdigest()},
                                                 sort_keys=True, separators=(",", ":"))
