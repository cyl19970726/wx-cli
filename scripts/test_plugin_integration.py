"""Native-hook contract exercised against compiled Rust synthetic journals."""
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest

from wx_agent.codex_plugin import CodexPlugin, PluginError
from scripts.test_local_channel import GROUP, TABLE, fixture


BINARY = Path(__file__).resolve().parents[1] / "target/debug/wx-cli"


class NativePluginIntegrationTests(unittest.TestCase):
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
                     "--session", "native-hook-fixture", "--source", self.source)
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
        self.terminal = "unknown"
        self.plugin = CodexPlugin(config, turn_reader=lambda *_: self.terminal)
        self.event = dict(hook_event_name="UserPromptSubmit", session_id="original-native-session",
                          cwd=str(self.root), turn_id="original-native-turn")

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

    def test_original_native_callback_business_decision_and_terminal_receipt_are_separate(self):
        self.co("inbox", "claim", "--conversation-id", self.cid, "--owner", "plugin-owner", "--expected-epoch", 0, success=False)
        self.assertEqual(self.delivery()["status"], "pending")
        response = self.plugin.hook(self.event)
        claim = self.delivery()
        self.assertEqual((claim["thread_id"], claim["turn_id"], claim["turn_state"]),
                         ("original-native-session", "original-native-turn", "recorded"))
        self.assertNotIn(claim["token"], json.dumps(response))
        self.assertEqual(self.plugin.hook(dict(self.event, hook_event_name="PostToolUse")), {})
        self.co("inbox", "complete", "--delivery-id", 1, "--token", claim["token"], "--decision", "skip")
        self.assertEqual(self.delivery()["turn_state"], "recorded")
        self.terminal = "completed"
        self.plugin.reconcile(self.plugin.routes[0])
        final = self.delivery()
        self.assertEqual((final["status"], final["turn_state"]), ("skipped", "completed"))
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM delivery").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM dispatch").fetchone()[0], 0)

    def test_takeover_fences_old_business_but_preserves_original_transport_for_handback(self):
        self.plugin.hook(self.event)
        original = self.delivery()
        self.co("takeover", "--conversation-id", self.cid, "--expected-epoch", 1)
        self.co("inbox", "complete", "--delivery-id", 1, "--token", original["token"], "--decision", "skip", success=False)
        self.co("handback", "--conversation-id", self.cid, "--expected-epoch", 2)
        self.assertEqual(self.plugin.hook(dict(self.event, turn_id="different-turn")), {})
        self.assertEqual((self.delivery()["epoch"], self.delivery()["turn_state"]), (1, "recorded"))
        self.value["bindings"][0]["expected_epoch"] = 3
        self.write(self.config, self.value)
        self.terminal = "completed"
        fresh = CodexPlugin(self.config, turn_reader=lambda *_: self.terminal)
        fresh.reconcile(fresh.routes[0])
        current = self.delivery()
        self.assertEqual((current["status"], current["epoch"], current["turn_state"]), ("pending", 3, None))
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            history = db.execute("SELECT thread_id,turn_id,turn_state FROM delivery_turn_history").fetchall()
        self.assertEqual(history, [("original-native-session", "original-native-turn", "completed")])


if __name__ == "__main__":
    unittest.main()
