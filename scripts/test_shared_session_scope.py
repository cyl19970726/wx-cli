#!/usr/bin/env python3
"""Synthetic same-Session grant and old-client SQL fence regression tests."""
import sqlite3
import unittest

from scripts import test_multi_conversation as multi_conversation


SESSION = "codex:synthetic:original"


class SharedSessionScopeTests(unittest.TestCase):
    setUp = multi_conversation.MultiConversationTests.setUp
    cli = multi_conversation.MultiConversationTests.cli
    coordinator = multi_conversation.MultiConversationTests.coordinator

    @classmethod
    def setUpClass(cls):
        if not multi_conversation.BINARY.is_file():
            raise unittest.SkipTest("compiled target/debug/wx-cli is required")

    def incoming_claim(self, name, sequence):
        channel = self.channels[name]
        with sqlite3.connect(channel["source"] / "message/message_0.db") as db:
            db.execute(
                f"INSERT INTO [{channel['table']}] VALUES (?, ?, 1, 1, ?, ?, NULL, 0)",
                (sequence, sequence + 100, 1700000000 + sequence,
                 b"synthetic_member:\nnew input"),
            )
        self.cli("channel", channel["state"], "sync")
        self.coordinator("inbox", "reconcile", "--conversation-id", channel["id"])
        return self.coordinator("inbox", "claim", "--conversation-id", channel["id"],
                                "--owner", "owner_" + name)

    def grant(self, name, epoch=None, owner=None, session=SESSION, okay=True):
        channel = self.channels[name]
        return self.coordinator(
            "inbox", "authorize-shared-session", "--session-ref", session,
            "--conversation-id", channel["id"], "--owner", owner or "owner_" + name,
            "--expected-epoch", epoch if epoch is not None else channel["epoch"],
            "--reason", "synthetic explicit two-target authorization", okay=okay,
        )

    def reserve(self, claim, session=SESSION, okay=True):
        return self.coordinator("inbox", "reserve-turn", "--delivery-id",
                                claim["delivery_id"], "--token", claim["token"],
                                "--session-ref", session, okay=okay)

    def finish_skip(self, claim):
        self.coordinator("inbox", "record-turn", "--delivery-id", claim["delivery_id"],
                         "--token", claim["token"], "--thread-id", "synthetic-original",
                         "--turn-id", "synthetic-turn")
        self.coordinator("inbox", "finish-turn", "--delivery-id", claim["delivery_id"],
                         "--token", claim["token"], "--status", "completed")
        self.coordinator("inbox", "complete", "--delivery-id", claim["delivery_id"],
                         "--token", claim["token"], "--decision", "skip")

    def test_repeated_opens_keep_schema_and_legacy_refusal(self):
        database = self.ui / "coordinator.sqlite3"
        group_id = self.channels["group"]["id"]
        private_id = self.channels["private"]["id"]
        with sqlite3.connect(database) as db:
            initial_version = db.execute("PRAGMA schema_version").fetchone()[0]
        for _ in range(3):
            self.coordinator("inbox", "reconcile", "--conversation-id", group_id)
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("PRAGMA schema_version").fetchone()[0], initial_version)
            db.execute("UPDATE binding SET session_ref='legacy:dedicated' WHERE conversation_id=?",
                       (group_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE binding SET session_ref='legacy:dedicated' WHERE conversation_id=?",
                           (private_id,))
            self.assertEqual(db.execute("SELECT conversation_id FROM session_scope "
                                        "WHERE session_ref='legacy:dedicated'").fetchone(),
                             (group_id,))
            # An old binary can recreate the unique index only while binding
            # refs are still unique. The next new-runtime open removes it once.
            db.execute("CREATE UNIQUE INDEX dedicated_session_per_conversation "
                       "ON binding(session_ref) WHERE session_ref IS NOT NULL")
            recreated_version = db.execute("PRAGMA schema_version").fetchone()[0]
        self.coordinator("inbox", "reconcile", "--conversation-id", group_id)
        with sqlite3.connect(database) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                         "AND name='dedicated_session_per_conversation'").fetchone())
            repaired_version = db.execute("PRAGMA schema_version").fetchone()[0]
            self.assertGreater(repaired_version, recreated_version)
        self.coordinator("inbox", "reconcile", "--conversation-id", group_id)
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("PRAGMA schema_version").fetchone()[0], repaired_version)

    def test_exact_shared_grants_and_old_client_reservation_fence(self):
        first = self.incoming_claim("group", 40)
        self.reserve(first)
        self.finish_skip(first)
        second = self.incoming_claim("private", 41)

        # Historical dedicated refusal remains the default, including direct SQL.
        self.reserve(second, okay=False)
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE binding SET session_ref=? WHERE conversation_id=?",
                           (SESSION, self.channels["private"]["id"]))
            self.assertEqual(db.execute("SELECT conversation_id FROM session_scope WHERE session_ref=?",
                                        (SESSION,)).fetchone(), (self.channels["group"]["id"],))

        self.assertTrue(self.grant("group")["authorized"])
        self.assertTrue(self.grant("private")["authorized"])
        self.assertTrue(self.grant("private")["already_authorized"])
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM shared_session_scope").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM control_event WHERE action='authorize_shared_session'").fetchone()[0], 2)
            db.execute("INSERT INTO binding(conversation_id,channel_state_dir,channel_id,session_id,"
                       "owner,input_mode,reply_policy) VALUES(?,?,?,?,?,'push','observe')",
                       ("synthetic_third", str(self.root / "third-state"), "channel_third",
                        "session_third", "owner_third"))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE binding SET session_ref=? WHERE conversation_id='synthetic_third'",
                           (SESSION,))
        self.reserve(second)
        self.finish_skip(second)
        self.assertEqual(self.coordinator("status", "--conversation-id",
                                          self.channels["private"]["id"])["owner_session_ref"], SESSION)

        third = self.incoming_claim("group", 42)
        self.reserve(third)
        self.finish_skip(third)
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE shared_session_scope SET epoch=100 WHERE conversation_id=?",
                           (self.channels["private"]["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM shared_session_scope WHERE conversation_id=?",
                           (self.channels["private"]["id"],))

        # A later epoch invalidates the old grant; direct SQL simulates an old
        # client skipping the Rust authorization check on an already-bound ref.
        channel = self.channels["private"]
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("UPDATE binding SET epoch=epoch+1 WHERE conversation_id=?", (channel["id"],))
        self.grant("private", epoch=channel["epoch"], okay=False)
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT conversation_id FROM session_scope WHERE session_ref=?",
                                        (SESSION,)).fetchone(), (self.channels["group"]["id"],))
            db.execute("INSERT INTO delivery(conversation_id,cursor_start,cursor_end,owner,epoch,status,"
                       "token,lease_until,created_at,updated_at) VALUES(?,?,?,?,?,'claimed',?,9999999999,1,1)",
                       (channel["id"], 900, 900, "owner_private", channel["epoch"] + 1,
                        "synthetic-old-client"))
            delivery_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE delivery SET turn_state='reserved',session_ref=? WHERE id=?",
                           (SESSION, delivery_id))
        self.assertTrue(self.grant("private", epoch=channel["epoch"] + 1)["authorized"])
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("UPDATE binding SET control='taken_over' WHERE conversation_id=?", (channel["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE delivery SET turn_state='reserved',session_ref=? WHERE id=?",
                           (SESSION, delivery_id))
            db.execute("UPDATE binding SET control='active',owner='other_owner' WHERE conversation_id=?",
                       (channel["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE delivery SET turn_state='reserved',session_ref=? WHERE id=?",
                           (SESSION, delivery_id))
            db.execute("UPDATE binding SET owner='owner_private',epoch=epoch+1 WHERE conversation_id=?",
                       (channel["id"],))
            db.execute("UPDATE binding SET session_ref=NULL WHERE session_ref=?", (SESSION,))
            db.execute("UPDATE delivery SET epoch=epoch+1 WHERE id=?", (delivery_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE delivery SET turn_state='reserved',session_ref=? WHERE id=?",
                           (SESSION, delivery_id))

    def test_bad_grant_inputs_and_ungranted_third_target(self):
        self.grant("group", epoch=0, okay=False)
        self.grant("group", owner="wrong_owner", okay=False)
        self.grant("group", session="bad ref", okay=False)
        self.grant("group", session="other:session", okay=True)
        first = self.incoming_claim("group", 50)
        self.reserve(first, session="other:session")
        self.finish_skip(first)
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            self.assertIsNone(db.execute("SELECT conversation_id FROM session_scope WHERE session_ref=?",
                                         ("other:session",)).fetchone())
        second = self.incoming_claim("private", 51)
        self.reserve(second, session="other:session", okay=False)

    def test_raw_sql_shared_slot_releases_after_finish(self):
        self.grant("group")
        self.grant("private")
        first = self.incoming_claim("group", 60)
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("INSERT INTO delivery(conversation_id,cursor_start,cursor_end,owner,epoch,"
                       "status,token,lease_until,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,'claimed','synthetic-old-client',9999999999,1,1)",
                       (self.channels["private"]["id"], 990, 990, "owner_private",
                        self.channels["private"]["epoch"]))
            second_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        # The partial unique index has no reserved row yet: this denial is the
        # shared Session's live-claim SQL fence, including an unset binding ref.
        self.reserve(first, okay=False)
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT turn_state,session_ref FROM delivery WHERE id=?",
                                        (first["delivery_id"],)).fetchone(), (None, None))
            db.execute("UPDATE delivery SET lease_until=1 WHERE id=?", (second_id,))
        self.reserve(first)
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("UPDATE binding SET session_ref=? WHERE conversation_id=?",
                       (SESSION, self.channels["private"]["id"]))
            db.execute("UPDATE delivery SET lease_until=9999999999 WHERE id=?", (second_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE delivery SET turn_state='reserved',session_ref=? WHERE id=?",
                           (SESSION, second_id))
            self.assertEqual(
                db.execute("SELECT status,turn_state,session_ref FROM delivery WHERE id=?",
                           (second_id,)).fetchone(),
                ("claimed", None, None),
            )
            # A repeat update of the first reservation remains idempotent.
            db.execute("UPDATE delivery SET turn_state='reserved',session_ref=? WHERE id=?",
                       (SESSION, first["delivery_id"]))
        self.finish_skip(first)
        with sqlite3.connect(self.ui / "coordinator.sqlite3") as db:
            db.execute("UPDATE delivery SET turn_state='reserved',session_ref=? WHERE id=?",
                       (SESSION, second_id))
            self.assertEqual(db.execute("SELECT turn_state FROM delivery WHERE id=?",
                                        (second_id,)).fetchone(), ("reserved",))


if __name__ == "__main__":
    unittest.main()
