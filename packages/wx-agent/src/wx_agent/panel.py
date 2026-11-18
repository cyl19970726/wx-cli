"""Local control panel for managed WeChat conversations.

The panel is an operator view over the existing Rust coordinator, registry and
outbox. It binds to loopback only, requires a per-start token on every API call
and exposes a small set of actions that already exist as CLI contracts:
pausing the shared desktop surface and approving an exact draft. It never
resumes or reviews the desktop, never replays an unknown send and never shows
lease or transport tokens.
"""
import argparse
from collections import Counter
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
from pathlib import Path
import re
import secrets
import sys

from . import conversations, dispatch, local

MAX_BODY = 16 * 1024
TODO_STATUSES = ("pending", "claimed", "deferred", "draft")
ATTENTION_DISPATCH = ("blocked", "unknown", "claimed")
DELIVERY_FIELDS = ("delivery_id", "conversation_id", "cursor_start", "cursor_end", "owner", "epoch",
                   "status", "turn_state", "draft_text", "reason", "condition", "due_at", "request_id")
MAX_EVENTS = 50
PAGE = Path(__file__).with_name("panel.html")
APPROVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}\Z")


class PanelError(Exception):
    def __init__(self, code, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.code = code
        self.status = status


def require(condition, code, message, status=HTTPStatus.BAD_REQUEST):
    if not condition:
        raise PanelError(code, message, status)


def _event_view(event):
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    content = message.get("content")
    text = content.get("Text") if isinstance(content, dict) else None
    replyable = (event.get("kind") == "message_observed" and event.get("direction") == "incoming" and
                 isinstance(text, str))
    return {"cursor": event.get("cursor"), "kind": event.get("kind"), "direction": event.get("direction"),
            "sender": message.get("sender"), "text": text if isinstance(text, str) else None,
            "content_type": None if isinstance(text, str) else (
                sorted(content)[0] if isinstance(content, dict) and content else None),
            "replyable": replyable}


class Panel:
    """Read and act through the same coordinator contracts the CLI uses."""

    def __init__(self, settings, approver="panel"):
        require(isinstance(approver, str) and bool(APPROVER.fullmatch(approver)),
                "approver", "approver must be 1..64 safe ASCII characters starting with a letter or digit")
        self.settings = settings
        self.root = dispatch.root_from_settings(settings)
        self.approver = approver

    def call(self, *parts):
        return dispatch.call(self.settings["wx_cli"], self.root, *parts)

    def _binding(self, cid):
        return self.call("status", "--conversation-id", cid)

    def overview(self):
        surface = self.call("dispatch", "status")
        bound = set(self.call("status").get("conversations", []))
        registry = conversations.registry(self.settings)
        items = []
        for cid in sorted(set(registry["entries"]) | bound):
            entry = registry["entries"].get(cid, {})
            item = {"conversation_id": cid, "name": entry.get("name"), "kind": entry.get("kind"),
                    "registered": cid in registry["entries"],
                    "state_origin": "legacy_controller" if entry.get("imported") else
                    entry.get("state_origin", "registry"),
                    "managed": cid in bound}
            if cid in bound:
                binding = self._binding(cid)
                for key in ("owner", "input", "reply_policy", "control", "epoch", "watermark",
                            "processed_cursor", "last_control_event"):
                    item[key] = binding.get(key)
                deliveries = self.call("inbox", "status", "--conversation-id", cid).get("deliveries", [])
                item["delivery_counts"] = dict(Counter(d.get("status") for d in deliveries))
                item["todos"] = [
                    {key: d.get(key) for key in ("delivery_id", "status", "cursor_start", "cursor_end",
                                                  "reason", "condition", "due_at", "has_draft", "turn_state")}
                    for d in deliveries if d.get("status") in TODO_STATUSES]
            items.append(item)
        dispatches = surface.get("dispatches", [])
        health_path = self.root / "inbox-health.json"
        health = local.read_json(health_path) if health_path.exists() else None
        return {"desktop": {"paused_reason": surface.get("paused_reason"),
                            "active_dispatch": surface.get("active_dispatch"),
                            "surface_epoch": surface.get("surface_epoch"),
                            "dispatch_counts": dict(Counter(d.get("status") for d in dispatches)),
                            "attention": [d for d in dispatches if d.get("status") in ATTENTION_DISPATCH],
                            "recent": dispatches[-20:]},
                "health": health, "conversations": items}

    def delivery(self, cid, delivery_id):
        require(type(delivery_id) is int and delivery_id > 0, "delivery_id", "positive delivery ID required")
        binding = self._binding(cid)
        shown = self.call("inbox", "show", "--delivery-id", delivery_id, "--owner", binding["owner"])
        require(shown.get("conversation_id") == cid, "delivery_drift", "delivery belongs to another conversation")
        _, entry = conversations.selected(self.settings, cid)
        start, end = shown["cursor_start"], shown["cursor_end"]
        limit = min(end - start + 1, MAX_EVENTS)
        events = conversations.events(self.settings, entry, start - 1, limit)
        return {"delivery": {key: shown.get(key) for key in DELIVERY_FIELDS},
                "binding": {key: binding.get(key) for key in ("owner", "epoch", "reply_policy", "control")},
                "events": [_event_view(item) for item in events],
                "truncated": end - start + 1 > limit}

    def approve_draft(self, cid, delivery_id, expected_epoch, cursor):
        for value, label in ((delivery_id, "delivery_id"), (expected_epoch, "expected_epoch"), (cursor, "cursor")):
            require(type(value) is int and value > 0, label, label + " must be a positive integer")
        binding = self._binding(cid)
        require(binding.get("epoch") == expected_epoch, "epoch_changed",
                "conversation owner epoch changed; reload before approving", HTTPStatus.CONFLICT)
        require(binding.get("control") == "active", "taken_over", "conversation is taken over",
                HTTPStatus.CONFLICT)
        require(binding.get("reply_policy") == "authorized", "policy",
                "reply policy is not authorized; change it explicitly before approving drafts",
                HTTPStatus.CONFLICT)
        owner = binding["owner"]
        shown = self.call("inbox", "show", "--delivery-id", delivery_id, "--owner", owner)
        require(shown.get("conversation_id") == cid, "delivery_drift", "delivery belongs to another conversation")
        if shown.get("status") == "replied" and shown.get("request_id"):
            return {"delivery_id": delivery_id, "status": "replied", "request_id": shown["request_id"],
                    "already_approved": True}
        require(shown.get("status") == "draft" and isinstance(shown.get("draft_text"), str),
                "not_draft", "delivery is not a draft todo", HTTPStatus.CONFLICT)
        require(shown["cursor_start"] <= cursor <= shown["cursor_end"], "cursor",
                "reply source must be inside the delivery's cursor range")
        sent = conversations.reply(self.settings, cursor, shown["draft_text"], cid,
                                   owner=owner, expected_epoch=expected_epoch, wait=False)
        approved = self.call("inbox", "approve-draft", "--delivery-id", delivery_id, "--owner", owner,
                             "--expected-epoch", expected_epoch, "--request-id", sent["request_id"],
                             "--approver", self.approver)
        return {"delivery_id": delivery_id, "status": approved.get("status"), "request_id": sent["request_id"],
                "send_status": sent.get("status"), "dispatch_id": sent.get("dispatch_id"),
                "already_approved": approved.get("already_approved", False)}

    def pause(self, reason):
        require(isinstance(reason, str) and 1 <= len(reason.strip()) and len(reason) <= 512,
                "reason", "pause requires a reason of 1..512 characters")
        return self.call("dispatch", "pause", "--reason", reason)


def make_handler(panel, token, allowed_hosts):
    page = PAGE.read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "wx-agent-panel"
        sys_version = ""

        def log_message(self, fmt, *args):
            # Request lines may carry IDs; keep them out of shared terminals.
            return

        def _send(self, status, body, content_type, extra=None):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status, value):
            self._send(status, json.dumps(value, ensure_ascii=False), "application/json; charset=utf-8")

        def _error(self, status, code, message):
            self._json(status, {"error": {"code": code, "message": message}})

        def _guard(self):
            if self.headers.get("Host") not in allowed_hosts:
                self._error(HTTPStatus.FORBIDDEN, "host", "unexpected Host header")
                return False
            return True

        def _authorized(self):
            supplied = self.headers.get("X-Panel-Token", "")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                self._error(HTTPStatus.UNAUTHORIZED, "token", "panel token required")
                return False
            return True

        def _run(self, action):
            try:
                self._json(HTTPStatus.OK, action())
            except PanelError as exc:
                self._error(exc.status, exc.code, str(exc))
            except conversations.ConversationError as exc:
                self._error(HTTPStatus.CONFLICT, exc.code, str(exc))
            except (dispatch.DispatchError, local.CommandError, local.StateError, OSError, ValueError,
                    KeyError, TypeError) as exc:
                self._error(HTTPStatus.BAD_GATEWAY, "local_state_error", str(exc) or type(exc).__name__)

        def do_GET(self):
            if not self._guard():
                return
            path, _, query = self.path.partition("?")
            if path == "/":
                nonce = secrets.token_urlsafe(16)
                csp = ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; "
                       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                       "frame-ancestors 'none'") % (nonce, nonce)
                self._send(HTTPStatus.OK, page.replace("__NONCE__", nonce), "text/html; charset=utf-8",
                           {"Content-Security-Policy": csp})
                return
            if not self._authorized():
                return
            parts = [part for part in path.split("/") if part]
            if parts == ["api", "overview"]:
                self._run(panel.overview)
            elif (len(parts) == 5 and parts[:2] == ["api", "conversations"] and parts[3] == "deliveries"
                  and parts[4].isdigit()):
                self._run(lambda: panel.delivery(parts[2], int(parts[4])))
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "unknown endpoint")

        def do_POST(self):
            if not self._guard() or not self._authorized():
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type", "JSON body required")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body", "body must be 1..16384 bytes")
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeError):
                self._error(HTTPStatus.BAD_REQUEST, "body", "invalid JSON")
                return
            if not isinstance(body, dict):
                self._error(HTTPStatus.BAD_REQUEST, "body", "JSON object required")
                return
            path = self.path.partition("?")[0]
            if path == "/api/approve-draft":
                self._run(lambda: panel.approve_draft(body.get("conversation_id"), body.get("delivery_id"),
                                                      body.get("expected_epoch"), body.get("cursor")))
            elif path == "/api/pause":
                self._run(lambda: panel.pause(body.get("reason")))
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "unknown endpoint")

    return Handler


def serve(panel, host="127.0.0.1", port=8765, token=None):
    address = ipaddress.ip_address(host)
    require(address.version == 4 and address.is_loopback, "host",
            "the panel binds to an IPv4 loopback address only")
    token = token or secrets.token_urlsafe(32)
    server = HTTPServer((host, port), None)
    port = server.server_address[1]
    allowed = {"%s:%d" % (host, port), "localhost:%d" % port}
    server.RequestHandlerClass = make_handler(panel, token, allowed)
    return server, "http://%s:%d/#token=%s" % (host, port, token)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wx-agent panel", description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--approver", default="panel")
    args = parser.parse_args(argv)
    try:
        panel = Panel(conversations.config(args.config), args.approver)
        server, url = serve(panel, args.host, args.port)
    except (PanelError, conversations.ConversationError, dispatch.DispatchError, local.StateError,
            OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": getattr(exc, "code", "local_state_error"), "message": str(exc)}},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"url": url}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
