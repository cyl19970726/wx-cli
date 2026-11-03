"""Adversarial regressions against synthetic Rust inbox records.

No native Codex host or desktop sender is started.
"""
import json
import sqlite3
import subprocess
import unittest

from scripts import test_plugin_integration as plugin_fixture


class PluginAdversarialTests(unittest.TestCase):
    def setUp(self):
        fixture = plugin_fixture.NativePluginIntegrationTests("test_original_native_callback_business_decision_and_terminal_receipt_are_separate")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def test_old_turn_finishes_during_takeover_and_preserves_history(self):
        case = self.fixture
        case.plugin.hook(case.event)
        case.co("takeover", "--conversation-id", case.cid, "--expected-epoch", 1)
        case.terminal = "completed"
        # An old transport receipt is still valid while business control is
        # taken over. Rust finish-turn archives it and requeues the delivery.
        case.plugin.reconcile(case.plugin.routes[0])
        with sqlite3.connect(case.ui / "coordinator.sqlite3") as db:
            history = db.execute("SELECT thread_id,turn_id,turn_state FROM delivery_turn_history").fetchall()
        self.assertEqual(history, [("original-native-session", "original-native-turn", "completed")])
        self.assertEqual(case.delivery()["status"], "pending")

    def test_stale_hook_during_takeover_does_not_claim_or_inject_context(self):
        case = self.fixture
        case.plugin.hook(case.event)
        case.co("takeover", "--conversation-id", case.cid, "--expected-epoch", 1)
        self.assertEqual(case.plugin.hook(dict(case.event, turn_id="different-turn")), {})
        old = case.delivery()
        self.assertEqual((old["status"], old["turn_state"], old["turn_id"]),
                         ("claimed", "recorded", "original-native-turn"))
        with sqlite3.connect(case.ui / "coordinator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM delivery").fetchone()[0], 1)

    def test_identical_human_prompt_does_not_prove_native_queue_receipt(self):
        case = self.fixture

        def queue_unknown(argv, **_):
            if argv[-1] == "--help":
                return type("Result", (), {"returncode": 0, "stdout": "queue --thread --message"})()
            raise subprocess.TimeoutExpired(argv, 20)

        # The reservation is made, but this test never grants a queue receipt.
        case.plugin.process_runner = queue_unknown
        case.plugin.notify_once()
        # A human can know these public identifiers. They cannot derive the
        # private transport token or the notification_ref bound to it.
        bare_metadata = "WXCLI_DELIVERY_V1 " + json.dumps(
            {"conversation_id": case.cid, "delivery_id": 1}, sort_keys=True, separators=(",", ":"))
        forged = dict(case.event, prompt=bare_metadata)
        case.plugin.hook(forged)
        self.assertIsNone(case.delivery()["turn_id"])


if __name__ == "__main__":
    unittest.main()
