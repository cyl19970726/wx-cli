"""Session routing and transport fences through compiled synthetic Rust."""
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest

from wx_agent.session_routes import SessionBindings, PluginError
from scripts.test_local_channel import GROUP, TABLE, fixture


BINARY = Path(__file__).resolve().parents[1] / "target/debug/wx-cli"


class SessionRouteIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise unittest.SkipTest("compiled wx-cli required")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.root.chmod(0o700)
        self.state, self.ui = self.root / "channel", self.root / "ui"
        self.ui.mkdir(mode=0o700)
        self.source = self.root / "synthetic"; fixture(self.source)
        self.run_cli("channel", self.state, "bind", "--account", "synthetic", "--talker", GROUP,
                     "--session", "session-route-fixture", "--source", self.source)
        with sqlite3.connect(self.state / "channel.sqlite3") as db:
            db.execute("UPDATE channel_binding SET self_wxid='synthetic_self'")
        self.run_cli("channel", self.state, "sync")
        self.cid = "d" * 32
        self.co("bind", "--conversation-id", self.cid, "--channel-state-dir", self.state,
                "--owner", "plugin-owner", "--input", "push", "--reply-policy", "observe")
        with sqlite3.connect(self.source / "message/message_0.db") as db:
            db.execute(f"INSERT INTO [{TABLE}] VALUES (5,901,1,1,1700000005,?,NULL,0)",
                       (b"synthetic_member:\nnew plugin task",))
        self.run_cli("channel", self.state, "sync")
        self.co("inbox", "reconcile", "--conversation-id", self.cid)
        registry = self.root / "registry"; registry.mkdir(mode=0o700)
        template = self.root / "template.json"; self.write(template, {"ui_dir": str(self.ui)})
        settings = self.root / "conversations.json"
        self.write(settings, dict(version=1, wx_cli=str(BINARY), registry_dir=str(registry), account="synthetic",
                                 source=str(self.source), sender_template=str(template), sender_executable="/bin/echo"))
        config = self.root / "plugin.json"
        self.value = dict(version=1, conversation_config=str(settings), wx_agent="/bin/echo", codex="/bin/echo",
                          bindings=[dict(session_id="original-native-session", conversation_id=self.cid,
                                         owner="plugin-owner", expected_epoch=1, project=str(self.root), agent_host=socket.gethostname())])
        self.write(config, self.value)
        self.config = config
        self.routes = SessionBindings(config)

    def reserve_original_turn(self):
        claim = self.co("inbox", "claim", "--conversation-id", self.cid,
                        "--owner", "plugin-owner", "--expected-epoch", 1)
        self.assertTrue(self.routes.reserve(self.routes.routes[0], claim))
        current = self.delivery()
        self.co("inbox", "record-turn", "--delivery-id", current["delivery_id"],
                "--token", current["transport_token"], "--thread-id", "original-native-session",
                "--turn-id", "synthetic-native-turn")
        return self.delivery()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def run_cli(self, section, state, *parts, success=True):
        process = subprocess.run([str(BINARY), section, "--state-dir", str(state), *map(str, parts)],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode == 0, success, process.stderr)
        return json.loads(process.stdout) if success else None

    def co(self, *parts, **kw):
        return self.run_cli("coordinator", self.ui, *parts, **kw)

    def delivery(self):
        return self.co("inbox", "show", "--delivery-id", 1, "--owner", "plugin-owner")

    def test_business_decision_and_original_transport_completion_are_separate(self):
        self.co("inbox", "claim", "--conversation-id", self.cid, "--owner", "plugin-owner",
                "--expected-epoch", 0, success=False)
        self.assertEqual(self.delivery()["status"], "pending")
        original = self.reserve_original_turn()
        self.assertEqual((original["thread_id"], original["turn_id"], original["turn_state"]),
                         ("original-native-session", "synthetic-native-turn", "recorded"))
        self.co("inbox", "complete", "--delivery-id", original["delivery_id"],
                "--token", original["token"], "--decision", "skip")
        self.assertEqual(self.delivery()["turn_state"], "recorded")
        self.co("inbox", "finish-turn", "--delivery-id", original["delivery_id"],
                "--token", original["transport_token"], "--status", "completed")
        final = self.delivery()
        self.assertEqual((final["status"], final["turn_state"]), ("skipped", "completed"))
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM delivery").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM dispatch").fetchone()[0], 0)

    def test_takeover_preserves_old_transport_and_fences_current_route(self):
        original = self.reserve_original_turn()
        self.co("takeover", "--conversation-id", self.cid, "--expected-epoch", 1)
        with self.assertRaisesRegex(PluginError, "binding_changed"):
            self.routes.binding(self.routes.routes[0])
        self.co("inbox", "complete", "--delivery-id", original["delivery_id"],
                "--token", original["token"], "--decision", "skip", success=False)
        self.assertEqual((self.delivery()["epoch"], self.delivery()["turn_state"]), (1, "recorded"))
        self.co("handback", "--conversation-id", self.cid, "--expected-epoch", 2)
        self.co("inbox", "finish-turn", "--delivery-id", original["delivery_id"],
                "--token", original["transport_token"], "--status", "completed")
        current = self.delivery()
        self.assertEqual((current["status"], current["epoch"], current["turn_state"]), ("pending", 3, None))
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            history = db.execute("SELECT thread_id,turn_id,turn_state FROM delivery_turn_history").fetchall()
        self.assertEqual(history, [("original-native-session", "synthetic-native-turn", "completed")])


if __name__ == "__main__":
    unittest.main()
