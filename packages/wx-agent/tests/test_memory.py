import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from wx_agent.memory import MEMORY_NAME, MemoryError, load_memory, main, update_memory


FAKE_CLI = '''#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
assert args[:3] == ["channel", "--state-dir", args[2]]
state = pathlib.Path(args[2])
fixture = json.loads((state / "fixture.json").read_text())
if args[3] == "status":
    print(json.dumps({"binding": fixture["binding"], "latest_cursor": max(
        [x["cursor"] for x in fixture["events"]], default=0)}))
elif args[3] == "events":
    after = int(args[args.index("--after") + 1])
    limit = int(args[args.index("--limit") + 1])
    for event in [x for x in fixture["events"] if x["cursor"] > after][:limit]:
        print(json.dumps(event))
else:
    sys.exit(2)
'''


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "private"
        self.state.mkdir(mode=0o700)
        self.cli = self.root / "wx-cli"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(0o700)
        self.binding = {"channel_id": "channel-1", "session_id": "session-1",
                        "account": "account-1", "talker": "group@chatroom"}
        self.events = [self.event(1), self.event(2), self.event(3, direction="outgoing")]
        self.fixture()

    def fixture(self):
        (self.state / "fixture.json").write_text(json.dumps({"binding": self.binding, "events": self.events}))

    def event(self, cursor, direction="incoming", **binding):
        fields = {**self.binding, **binding}
        return {"cursor": cursor, "kind": "message_observed", "direction": direction,
                "server_id": cursor, "message": {"server_id": cursor, "talker": fields["talker"]},
                **fields}

    def doc(self, cursor=1):
        return {"version": 1, "binding": self.binding.copy(), "summary": "Prefers Tuesday lessons",
                "summary_source_cursors": [cursor],
                "confirmed": [{"text": "Tuesday works", "source_cursors": [cursor]}],
                "open_questions": [{"text": "Which time zone?", "source_cursors": [cursor]}]}

    def test_persists_privately_across_restart_with_revision_cas(self):
        self.assertEqual(load_memory(self.state, self.cli)["revision"], 0)
        saved = update_memory(self.state, self.cli, self.doc(), 0)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(load_memory(self.state, self.cli), saved)
        path = self.state / MEMORY_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(x.name for x in self.state.iterdir() if x.name.startswith(".agent-memory-")), [])
        with self.assertRaisesRegex(MemoryError, "revision changed"):
            update_memory(self.state, self.cli, self.doc(2), 0)
        self.assertEqual(load_memory(self.state, self.cli), saved)
        self.assertEqual(update_memory(self.state, self.cli, self.doc(2), 1)["revision"], 2)

    def test_rejects_binding_drift_and_cross_session_reference(self):
        update_memory(self.state, self.cli, self.doc(), 0)
        self.binding = {**self.binding, "session_id": "session-2"}
        self.events = [self.event(1)]
        self.fixture()
        with self.assertRaisesRegex(MemoryError, "binding differs"):
            load_memory(self.state, self.cli)
        with self.assertRaisesRegex(MemoryError, "binding differs"):
            update_memory(self.state, self.cli, self.doc(), 1)

    def test_rejects_missing_future_outgoing_and_mismatched_sources(self):
        for cursor, error in [(3, "not an incoming"), (4, "future"), (99, "future")]:
            with self.subTest(cursor=cursor), self.assertRaisesRegex(MemoryError, error):
                update_memory(self.state, self.cli, self.doc(cursor), 0)
        self.events = [self.event(1), self.event(3)]
        self.fixture()
        with self.assertRaisesRegex(MemoryError, "missing"):
            update_memory(self.state, self.cli, self.doc(2), 0)
        self.events = [self.event(1, session_id="other")]
        self.fixture()
        with self.assertRaisesRegex(MemoryError, "not an incoming"):
            update_memory(self.state, self.cli, self.doc(1), 0)

    def test_rejects_unprivate_state_and_memory_links(self):
        self.state.chmod(0o755)
        with self.assertRaises(MemoryError):
            load_memory(self.state, self.cli)
        self.state.chmod(0o700)
        target = self.root / "target"
        target.write_text("{}")
        target.chmod(0o600)
        memory = self.state / MEMORY_NAME
        memory.symlink_to(target)
        with self.assertRaises(MemoryError):
            load_memory(self.state, self.cli)
        memory.unlink()
        os.link(target, memory)
        with self.assertRaises(MemoryError):
            update_memory(self.state, self.cli, self.doc(), 0)

    def test_cli_private_input_and_atomic_rejection(self):
        source = self.root / "update.json"
        source.write_text(json.dumps(self.doc()))
        source.chmod(0o644)
        self.assertEqual(main(["update", "--state-dir", str(self.state), "--wx-cli", str(self.cli),
                               "--file", str(source), "--expected-revision", "0"]), 1)
        self.assertFalse((self.state / MEMORY_NAME).exists())
        source.chmod(0o600)
        self.assertEqual(main(["update", "--state-dir", str(self.state), "--wx-cli", str(self.cli),
                               "--file", str(source), "--expected-revision", "0"]), 0)
        self.assertEqual(load_memory(self.state, self.cli)["revision"], 1)

    def test_absolute_paths_and_group_binding_required(self):
        with self.assertRaisesRegex(MemoryError, "absolute state"):
            load_memory("relative-state", self.cli)
        with self.assertRaisesRegex(MemoryError, "absolute wx-cli"):
            load_memory(self.state, "wx-cli")
        self.binding["talker"] = "person"
        self.fixture()
        with self.assertRaisesRegex(MemoryError, "one group"):
            load_memory(self.state, self.cli)

    def test_bounded_command_output_and_timeout(self):
        self.cli.write_text("#!/usr/bin/env python3\nprint('x' * 100001)\n")
        with self.assertRaisesRegex(MemoryError, "channel read failed"):
            load_memory(self.state, self.cli)
        with patch("wx_agent.channel_agent.command", side_effect=subprocess.TimeoutExpired("wx-cli", 5)):
            with self.assertRaisesRegex(MemoryError, "channel read failed"):
                load_memory(self.state, self.cli)

    def test_corrupt_memory_and_boolean_versions_fail_closed(self):
        path = self.state / MEMORY_NAME
        path.write_text("{")
        path.chmod(0o600)
        with self.assertRaisesRegex(MemoryError, "invalid JSON"):
            load_memory(self.state, self.cli)
        path.unlink()
        document = self.doc()
        document["version"] = True
        with self.assertRaisesRegex(MemoryError, "invalid update schema"):
            update_memory(self.state, self.cli, document, 0)
        document["version"] = 1
        saved = update_memory(self.state, self.cli, document, 0)
        saved["version"] = True
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(MemoryError, "invalid memory version"):
            load_memory(self.state, self.cli)
        saved["version"] = 1
        saved["summary_source_cursors"] = [4]
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(MemoryError, "stored source cursor is in the future"):
            load_memory(self.state, self.cli)

    def test_imported_valid_schema_with_bad_provenance_fails_closed(self):
        path = self.state / MEMORY_NAME
        cases = [
            ([self.event(1), self.event(3)], 2, "missing"),
            ([self.event(1), self.event(2, direction="outgoing")], 2, "not an incoming"),
            ([self.event(1, session_id="other")], 1, "not an incoming"),
        ]
        for events, cursor, error in cases:
            with self.subTest(error=error):
                self.events = events
                self.fixture()
                imported = {**self.doc(cursor), "revision": 1}
                path.write_text(json.dumps(imported))
                path.chmod(0o600)
                with self.assertRaisesRegex(MemoryError, error):
                    load_memory(self.state, self.cli)


if __name__ == "__main__":
    unittest.main()
