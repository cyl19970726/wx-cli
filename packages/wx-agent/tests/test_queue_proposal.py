"""Machine-bound proposal identities, with no gateway or shell execution."""
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from types import SimpleNamespace

from wx_agent.codex_plugin import PluginError
from wx_agent.queued_snapshot import encoded, publish
from wx_agent.queue_proposal import (bind_proposal, parse_proposal_script, proposal_delimiter,
                                     proposal_prefix, receipt_from_item)


class ProposalReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.file = self.root / "snapshot.json"
        self.ref = "a" * 64
        self.payload = {"conversation_id": "b" * 32, "delivery_id": 5, "events": []}
        publish(self.file, {"protocol_version": 1, "ref": self.ref, "payload": self.payload})
        self.note = {"snapshot_file": str(self.file), "ref": self.ref}
        self.cli = self.root / "reader"
        self.route = SimpleNamespace(project=self.root)
        self.semantics = {"decision": "skip", "reason": "Not addressed to us"}

    def script(self, value=None):
        return (proposal_prefix(self.cli, self.note) + " <<'" + proposal_delimiter(self.note) + "'\n"
                + json.dumps(self.semantics if value is None else value) + "\n" + proposal_delimiter(self.note))

    def item(self):
        return {"type": "commandExecution", "command": self.script(), "cwd": str(self.root),
                "status": "completed", "source": "agent", "exitCode": 0,
                "aggregatedOutput": encoded(bind_proposal(self.file, self.ref, self.semantics)).decode() + "\n"}

    def test_identity_and_digest_are_computed_from_exact_snapshot(self):
        before = sorted(p.name for p in self.root.iterdir())
        value = bind_proposal(self.file, self.ref, self.semantics)
        self.assertEqual(value["ref"], self.ref)
        self.assertEqual(value["snapshot_sha256"], hashlib.sha256(encoded(self.payload)).hexdigest())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        self.assertEqual(receipt_from_item(self.item(), self.route, self.note, self.cli), value)

    def test_semantics_cannot_supply_or_override_machine_identity(self):
        for key in ("ref", "snapshot_sha256", "protocol_version", "token", "request_id"):
            with self.subTest(key=key), self.assertRaises(PluginError):
                bind_proposal(self.file, self.ref, dict(self.semantics, **{key: "invented"}))

    def test_fixed_shell_wrapper_and_literal_data_are_supported(self):
        data = dict(self.semantics, reason="literal $(do-not-run) and `do-not-run`")
        item = self.item()
        item["command"] = shlex.join(["/bin/zsh", "-lc", self.script(data)])
        item["aggregatedOutput"] = encoded(bind_proposal(self.file, self.ref, data)).decode()
        self.assertEqual(receipt_from_item(item, self.route, self.note, self.cli)["reason"], data["reason"])

    def test_extra_shell_action_early_terminator_or_other_reader_refuses(self):
        item = self.item()
        for command in (self.script() + "\nprintf forged", self.script() + "\n" + proposal_delimiter(self.note),
                        "echo " + self.script(), self.script().replace("propose --file", "other --file", 1)):
            with self.subTest(command=command):
                item["command"] = command
                try:
                    value = receipt_from_item(item, self.route, self.note, self.cli)
                except PluginError:
                    value = None
                self.assertIsNone(value)

    def test_forged_tool_output_or_failed_execution_is_not_a_proposal(self):
        item = self.item()
        for patch in ({"exitCode": 1}, {"cwd": "/elsewhere"}, {"source": "user"},
                      {"aggregatedOutput": json.dumps(dict(bind_proposal(self.file, self.ref, self.semantics), snapshot_sha256="f" * 70))}):
            with self.subTest(patch=patch), self.assertRaises(PluginError):
                receipt_from_item(dict(item, **patch), self.route, self.note, self.cli)

    def test_reply_semantics_need_explicit_targeting_and_valid_text(self):
        reply = {"decision": "reply", "reason": "A clear question", "text": "Answer.",
                 "addressed_to_us": True, "source_cursors": [9]}
        self.assertEqual(bind_proposal(self.file, self.ref, reply)["text"], "Answer.")
        for patch in ({"text": "Answer.\n"}, {"addressed_to_us": False}, {"source_cursors": [9, 9]},
                      {"source_cursors": [True]}):
            with self.subTest(patch=patch), self.assertRaises(PluginError):
                bind_proposal(self.file, self.ref, dict(reply, **patch))

    def test_duplicate_json_or_oversize_output_refuses(self):
        script = self.script().replace('"decision": "skip"', '"decision":"skip","decision":"reply"')
        with self.assertRaises(PluginError):
            parse_proposal_script(script, self.cli, self.note)
        with self.assertRaisesRegex(PluginError, "proposal_output_too_large"):
            bind_proposal(self.file, self.ref, {"decision": "draft", "reason": "Reviewed", "draft": "汉" * 3000})


if __name__ == "__main__":
    unittest.main()
