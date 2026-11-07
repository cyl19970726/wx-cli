"""Routing and lease fences without native callbacks, a model, or a sender."""
import copy
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest

from wx_agent.session_routes import GatewayInbox, PluginError, Route, SessionBindings


class Inbox:
    def __init__(self):
        self.calls = []
        self.binding = dict(conversation_id="a" * 32, owner="owner", epoch=1,
                            control="active", input="push", owner_session_ref=None)
        self.delivery = dict(delivery_id=7, conversation_id="a" * 32, owner="owner", epoch=1,
                             status="claimed", token="synthetic-token", turn_state=None)
        self.reserved = False

    def call(self, *parts):
        self.calls.append(parts)
        if parts[0] == "status":
            return copy.deepcopy(self.binding)
        if parts[:2] == ("inbox", "status"):
            return {"deliveries": [copy.deepcopy(self.delivery)]}
        if parts[:2] == ("inbox", "show"):
            return copy.deepcopy(self.delivery)
        if parts[:2] == ("inbox", "reserve-turn"):
            return dict(delivery_id=7, turn_state="reserved", already_reserved=self.reserved)
        raise AssertionError(parts)


class SessionRouteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = self.root / "routes.json"
        self.value = dict(version=1, conversation_config=str(self.root / "channel.json"),
                          wx_agent="/bin/echo", codex="/bin/echo", bindings=[dict(
                              session_id="original-session", conversation_id="a" * 32, owner="owner",
                              expected_epoch=1, project=str(self.root), agent_host=socket.gethostname())])
        self.inbox = Inbox()
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.value))
        self.config.chmod(0o600)

    def routes(self, **options):
        return SessionBindings(self.config, inbox=self.inbox, **options)

    def test_configuration_does_not_claim_or_contact_native_host(self):
        routes = self.routes()
        self.assertEqual(routes.routes[0].session_id, "original-session")
        self.assertEqual(self.inbox.calls, [])
        self.assertIsNone(importlib.util.find_spec("wx_agent.codex_plugin"))

    def test_notification_wire_identity_matches_pre_removal_fixture(self):
        route = Route("original-session", "a" * 32, "owner", 1,
                      Path("/synthetic/project"), "synthetic-host")
        expected = ('WXCLI_DELIVERY_V1 {"conversation_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                    '"delivery_id":7,"notification_ref":'
                    '"76d0ab9f9fedac262ed3a886ded83de9556a91328a731e63db01ba6edf89e32f"}')
        self.assertEqual(SessionBindings.notification(route, 7, "synthetic-transport-token"), expected)
        self.assertNotIn("synthetic-transport-token", expected)

    def test_notification_requires_the_original_transport_token(self):
        for value in (None, "", 7):
            with self.subTest(value=value), self.assertRaises(PluginError):
                SessionBindings.notification(self.routes().routes[0], 7, value)

    def test_binding_drift_fails_before_any_lease_operation(self):
        changes = ({"epoch": 2}, {"owner": "successor"}, {"control": "taken_over"},
                   {"input": "pull"}, {"owner_session_ref": "codex:another:session"},
                   {"conversation_id": "b" * 32})
        for change in changes:
            with self.subTest(change=change):
                inbox = Inbox()
                inbox.binding.update(change)
                routes = SessionBindings(self.config, inbox=inbox)
                with self.assertRaisesRegex(PluginError, "binding_changed"):
                    routes.deliveries(routes.routes[0])
                self.assertEqual(inbox.calls, [("status", "--conversation-id", "a" * 32)])

    def test_show_keeps_original_delivery_and_current_owner_fences(self):
        routes = self.routes()
        for change in ({"delivery_id": 8}, {"conversation_id": "b" * 32},
                       {"owner": "other"}, {"epoch": 2}):
            self.inbox.delivery = dict(delivery_id=7, conversation_id="a" * 32, owner="owner", epoch=1)
            self.inbox.delivery.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(PluginError, "delivery_changed"):
                routes.show(routes.routes[0], 7)

    def test_reservation_uses_existing_lease_and_does_not_queue_a_notice(self):
        routes = self.routes()
        self.assertTrue(routes.reserve(routes.routes[0], self.inbox.delivery))
        self.assertEqual(self.inbox.calls, [("inbox", "reserve-turn", "--delivery-id", 7,
                                           "--token", "synthetic-token", "--session-ref", routes.routes[0].session_ref)])
        self.inbox.reserved = True
        self.assertFalse(routes.reserve(routes.routes[0], self.inbox.delivery))

    def test_changed_claim_cannot_reserve(self):
        routes = self.routes()
        for patch in ({"conversation_id": "b" * 32}, {"owner": "other"}, {"epoch": 2}, {"token": ""}):
            value = dict(self.inbox.delivery, **patch)
            with self.subTest(patch=patch), self.assertRaisesRegex(PluginError, "claim_changed"):
                routes.reserve(routes.routes[0], value)
        self.assertEqual(self.inbox.calls, [])

    def test_shared_session_is_explicit_and_keeps_one_actual_reference(self):
        self.value["bindings"].append(dict(self.value["bindings"][0], conversation_id="b" * 32))
        self.save()
        with self.assertRaisesRegex(PluginError, "ambiguous_binding"):
            self.routes()
        self.value["allow_shared_session"] = True
        self.save()
        shared = self.routes(allow_shared_routes=True)
        self.assertEqual(len({route.session_ref for route in shared.routes}), 1)
        self.value["bindings"][1]["agent_host"] = "other-host"
        self.save()
        with self.assertRaisesRegex(PluginError, "shared_session_identity_changed"):
            self.routes(allow_shared_routes=True)

    def test_removed_callback_transport_fields_are_rejected_before_use(self):
        for key, value in (("remote", "unix:///tmp/old.sock"),
                           ("remote_auth_token_env", "OLD_TOKEN"), ("resume_queued", False)):
            with self.subTest(key=key):
                self.value[key] = value
                self.save()
                with self.assertRaisesRegex(PluginError, "hook_transport_removed"):
                    self.routes()
                del self.value[key]
        self.assertEqual(self.inbox.calls, [])

    def test_gateway_bridge_does_not_let_the_client_choose_owner_or_session(self):
        route = self.routes().routes[0]
        calls = []
        class Client:
            def request(self, method, params):
                calls.append((method, params))
                return dict(conversation_id=route.conversation_id, delivery_id=7, status="claimed")
        bridge = GatewayInbox(Client(), [route])
        bridge.call("inbox", "claim", "--conversation-id", route.conversation_id,
                    "--owner", route.owner, "--expected-epoch", 1, "--session-ref", route.session_ref)
        self.assertEqual(calls, [("inbox_claim", {"conversation_id": route.conversation_id})])
        for flag, value in (("--owner", "other"), ("--expected-epoch", 2), ("--session-ref", "codex:other")):
            with self.subTest(flag=flag), self.assertRaises(PluginError):
                bridge.call("inbox", "claim", "--conversation-id", route.conversation_id, flag, value)
        self.assertEqual(len(calls), 1)

    def test_gateway_rejects_cross_target_delivery_metadata(self):
        route = self.routes().routes[0]
        class Client:
            def request(self, *_):
                return dict(conversation_id=route.conversation_id,
                            deliveries=[dict(delivery_id=7, conversation_id="b" * 32)])
        with self.assertRaisesRegex(PluginError, "gateway_scope_changed"):
            GatewayInbox(Client(), [route]).call("inbox", "status", "--conversation-id", route.conversation_id)

    def test_gateway_refuses_one_delivery_id_reused_across_targets(self):
        route = self.routes().routes[0]
        other = Route(route.session_id, "b" * 32, route.owner, 1, route.project, route.agent_host)
        bridge = GatewayInbox(None, [route, other])
        bridge.remember(route.conversation_id, {"delivery_id": 7})
        with self.assertRaisesRegex(PluginError, "gateway_scope_changed"):
            bridge.remember(other.conversation_id, {"delivery_id": 7})


if __name__ == "__main__":
    unittest.main()
