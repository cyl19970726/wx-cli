#!/usr/bin/env python3
"""Compiled Rust inbox regression for canonical nonparticipant notifications."""
import json
import sqlite3
import unittest

from scripts import test_multi_conversation as multi


class SystemInboxTests(unittest.TestCase):
    setUp = multi.MultiConversationTests.setUp
    cli = multi.MultiConversationTests.cli
    coordinator = multi.MultiConversationTests.coordinator

    @classmethod
    def setUpClass(cls):
        if not multi.BINARY.is_file():
            raise unittest.SkipTest("compiled target/debug/wx-cli is required")

    def append(self, *messages):
        channel = self.channels["group"]
        dbpath = channel["state"] / "channel.sqlite3"
        with sqlite3.connect(dbpath) as db:
            start = db.execute("SELECT COALESCE(MAX(cursor),0) FROM channel_events").fetchone()[0]
            for offset, (sender, msg_type, content) in enumerate(messages, 1):
                message = {"talker": channel["talker"], "sender": sender,
                           "msg_type": msg_type, "create_time": 1700001000 + offset,
                           "content": content}
                db.execute("INSERT INTO channel_events(kind,server_id,message_json) "
                           "VALUES('message_observed',?,?)",
                           (8000 + start + offset, json.dumps(message)))
        return start, start + len(messages)

    def binding(self):
        return self.coordinator("status", "--conversation-id", self.channels["group"]["id"])

    def deliveries(self):
        return self.coordinator("inbox", "status", "--conversation-id",
                                self.channels["group"]["id"])["deliveries"]

    def logical_state(self):
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name")]
            return {table: tuple(db.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall())
                    for table in tables}

    def assert_refused_without_mutation(self, sender, msg_type, content):
        channel = self.channels["group"]
        self.append(("synthetic_member", 1, {"Text": "valid earlier question"}),
                    (sender, msg_type, content))
        before = self.logical_state()
        watermark = self.binding()["watermark"]
        error = self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"],
                                 okay=False)
        self.assertTrue(error)
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.binding()["watermark"], watermark)
        self.assertEqual(self.deliveries(), [])

    def test_mixed_human_system_revoke_human_keeps_contiguous_delivery_context(self):
        channel = self.channels["group"]
        start, end = self.append(
            ("synthetic_member", 1, {"Text": "first question"}),
            ("", 10000, {"System": "synthetic membership notice"}),
            ("", 10002, {"Revoke": "synthetic recall notice"}),
            ("synthetic_member", 1, {"Text": "second question"}),
        )
        result = self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"])
        self.assertEqual((result["created"], result["watermark"], result["possible_manual_outgoing"]),
                         (1, end, False))
        claim = self.coordinator("inbox", "claim", "--conversation-id", channel["id"],
                                 "--owner", "owner_group")
        self.assertEqual((claim["cursor_start"], claim["cursor_end"]), (start + 1, end))
        with sqlite3.connect(channel["state"] / "channel.sqlite3") as db:
            rows = db.execute("SELECT message_json FROM channel_events WHERE cursor>? AND cursor<=? "
                              "ORDER BY cursor", (start, end)).fetchall()
        self.assertEqual([json.loads(row[0])["msg_type"] for row in rows], [1, 10000, 10002, 1])
        self.assertEqual(self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"])
                         ["created"], 0)

    def test_system_only_including_self_marker_never_delivers_or_takes_over(self):
        channel = self.channels["group"]
        before = self.binding()
        start, end = self.append(
            ("", 10000, {"System": "synthetic notice"}),
            ("synthetic_self", 10000, {"System": "synthetic self-marked notice"}),
            ("", 10002, {"Revoke": "synthetic recall"}),
        )
        result = self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"])
        after = self.binding()
        self.assertEqual((result["created"], result["watermark"], result["possible_manual_outgoing"]),
                         (0, end, False))
        self.assertEqual((after["epoch"], after["control"]), (before["epoch"], "active"))
        self.assertEqual(self.deliveries(), [])
        with sqlite3.connect(channel["state"] / "channel.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM channel_events WHERE cursor>? AND cursor<=?",
                                        (start, end)).fetchone()[0], 3)

    def test_known_sender_voice_preserves_participant_delivery(self):
        channel = self.channels["group"]
        start, end = self.append(("synthetic_member", 34, "Voice"))
        result = self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"])
        self.assertEqual((result["created"], result["watermark"]), (1, end))
        claim = self.coordinator("inbox", "claim", "--conversation-id", channel["id"],
                                 "--owner", "owner_group")
        self.assertEqual((claim["cursor_start"], claim["cursor_end"]), (start + 1, end))

    def test_text_voice_system_text_keeps_participant_batch_and_context(self):
        channel = self.channels["group"]
        start, end = self.append(
            ("synthetic_member", 1, {"Text": "first question"}),
            ("synthetic_member", 34, "Voice"),
            ("", 10000, {"System": "synthetic notice"}),
            ("synthetic_member", 1, {"Text": "second question"}),
        )
        result = self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"])
        self.assertEqual((result["created"], result["watermark"]), (1, end))
        claim = self.coordinator("inbox", "claim", "--conversation-id", channel["id"],
                                 "--owner", "owner_group")
        self.assertEqual((claim["cursor_start"], claim["cursor_end"]), (start + 1, end))
        with sqlite3.connect(channel["state"] / "channel.sqlite3") as db:
            rows = db.execute("SELECT message_json FROM channel_events WHERE cursor>? AND cursor<=? "
                              "ORDER BY cursor", (start, end)).fetchall()
        self.assertEqual([json.loads(row[0])["msg_type"] for row in rows], [1, 34, 10000, 1])

    def test_empty_sender_text_refuses_entire_transaction(self):
        self.assert_refused_without_mutation("", 1, {"Text": "unresolved participant"})

    def test_empty_sender_quote_refuses_entire_transaction(self):
        self.assert_refused_without_mutation("", 49, {"Quote": {"text": "unresolved"}})

    def test_empty_sender_voice_refuses_entire_transaction(self):
        self.assert_refused_without_mutation("", 34, "Voice")

    def test_empty_sender_unsupported_refuses_entire_transaction(self):
        self.assert_refused_without_mutation("", 99999, {"Unknown": {"msg_type": 99999, "raw": "x"}})

    def test_spoofed_system_tag_with_text_type_refuses(self):
        self.assert_refused_without_mutation("synthetic_member", 1, {"System": "spoof"})

    def test_spoofed_system_string_with_voice_type_refuses(self):
        self.assert_refused_without_mutation("synthetic_member", 34, "System")

    def test_system_type_with_text_tag_refuses(self):
        self.assert_refused_without_mutation("", 10000, {"Text": "spoof"})

    def test_revoke_type_with_system_tag_refuses(self):
        self.assert_refused_without_mutation("", 10002, {"System": "spoof"})

    def test_malformed_system_value_refuses(self):
        self.assert_refused_without_mutation("", 10000, {"System": {"unexpected": "object"}})

    def test_malformed_message_type_refuses(self):
        self.assert_refused_without_mutation("synthetic_self", "10000", {"System": "spoof"})


if __name__ == "__main__":
    unittest.main()
