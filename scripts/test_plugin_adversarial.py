"""Queue correlation and legacy-reservation fences through real synthetic Rust.

The native surface is a fixture; no actual host, network or GUI is invoked.
"""
import unittest

from scripts import test_queue_snapshot_integration as queue_fixture
from wx_agent.queued_snapshot import read_private


class QueueAdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        queue_fixture.SnapshotGatewayIntegrationTests.setUpClass()

    def setUp(self):
        self.case = queue_fixture.SnapshotGatewayIntegrationTests(
            "test_actual_gateway_rust_paginated_snapshot_skip_and_finish")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.seed_frozen_delivery()

    def shown(self, note):
        case = self.case
        return case.gateway.clients[case.app].request("inbox_show", {
            "conversation_id": case.cid, "delivery_id": note["delivery_id"]})

    def test_matching_human_text_without_native_client_identity_is_not_receipt(self):
        case = self.case
        case.plugin.notify_once()
        note = read_private(next(case.runtime.glob("*.transport.json")))
        case.execute_reader_turn(note)
        # Text can be copied by a human; only the native correlated submission
        # identifies the Session turn that actually received our one notice.
        case.native.thread["turns"][0]["items"][0]["clientId"] = "unrelated-human-input"
        result = case.plugin.reconcile()
        self.assertEqual(result["notifications"][0]["status"], "requires_inspection")
        current = self.shown(note)
        self.assertEqual((current["status"], current["turn_state"], current["turn_id"]),
                         ("claimed", "reserved", None))
        case.new_plugin().notify_once()
        self.assertEqual(len(case.native.queue_calls), 1)
        self.assertFalse((case.root / "driver-events.jsonl").exists())

    def test_takeover_refuses_new_actions_but_keeps_original_reservation(self):
        case = self.case
        case.plugin.notify_once()
        note = read_private(next(case.runtime.glob("*.transport.json")))
        old = self.shown(note)
        fixture = case.gateway.fixture
        fixture.cli("coordinator", fixture.ui, "takeover",
                    "--conversation-id", case.cid, "--expected-epoch", 1)
        result = case.new_plugin().notify_once()
        self.assertEqual(result["notifications"][0]["status"], "requires_inspection")
        current = fixture.cli("coordinator", fixture.ui, "inbox", "show",
                              "--delivery-id", note["delivery_id"],
                              "--owner", "synthetic_app_a_owner")
        self.assertEqual((current["epoch"], current["turn_state"], current["transport_token"]),
                         (old["epoch"], "reserved", old["transport_token"]))
        self.assertEqual(len(case.native.queue_calls), 1)
        self.assertFalse((case.root / "driver-events.jsonl").exists())

    def test_legacy_reservation_without_snapshot_is_not_adopted_or_requeued(self):
        case = self.case
        client = case.gateway.clients[case.app]
        claim = client.request("inbox_claim", {"conversation_id": case.cid})
        client.request("reserve_turn", {"conversation_id": case.cid,
            "delivery_id": claim["delivery_id"], "token": claim["token"]})
        result = case.plugin.notify_once()
        self.assertEqual(result["notifications"][0]["status"], "original_transport_pending")
        self.assertEqual(case.native.queue_calls, [])
        self.assertEqual(list(case.runtime.iterdir()), [])
        shown = self.shown(claim)
        self.assertEqual((shown["status"], shown["turn_state"], shown["token"]),
                         ("claimed", "reserved", claim["token"]))


if __name__ == "__main__":
    unittest.main()
