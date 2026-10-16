"""Synthetic conversation registry and guarded outbox tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wx_agent import conversations as conv


def write_private(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


class FakeCLI:
    def __init__(self, settings):
        self.settings = settings
        self.metadata = [
            {"conversation_id": conv.conversation_id(settings["account"], talker),
             "talker": talker, "name": name, "kind": kind}
            for talker, name, kind in (("one@chatroom", "Team One", "group"),
                                       ("two@chatroom", "Team Two", "group"),
                                       ("wxid_friend", "Friend", "private"))]
        self.channels = {}
        self.outboxes = {}
        self.events = {}
        self.synced = []
        self.sent = []
        self.on_queue = None

    def __call__(self, argv, data=None, timeout=60, limit=None):
        if argv[1:3] == ["conversations", "find"]:
            if "--id" in argv:
                query = argv[argv.index("--id") + 1]
                result = [item for item in self.metadata if item["conversation_id"] == query]
                kind = "id" if result else "none"
            else:
                query = argv[argv.index("--name") + 1].casefold()
                choices = [item for item in self.metadata
                           if argv[argv.index("--kind") + 1] in ("all", item["kind"])]
                exact = [item for item in choices if item["name"].casefold() == query]
                if "--all-matches" in argv:
                    result = [item for item in choices if query in item["name"].casefold()]
                    kind = "partial" if len(result) > len(exact) else "exact" if result else "none"
                elif exact:
                    result, kind = exact, "exact"
                elif "--exact" in argv:
                    result, kind = [], "none"
                else:
                    result = [item for item in choices if query in item["name"].casefold()]
                    kind = "partial" if result else "none"
            return json.dumps({"matches": result, "match_kind": kind})
        if argv[1] not in ("channel", "outbox"):
            return json.dumps({"focused": True})
        state = argv[argv.index("--state-dir") + 1]
        action = argv[argv.index("--state-dir") + 2]
        if argv[1] == "channel":
            if action == "bind":
                talker = argv[argv.index("--talker") + 1]
                binding = {"channel_id": "channel-" + talker, "session_id": argv[argv.index("--session") + 1],
                           "account": self.settings["account"], "talker": talker,
                           "source": self.settings["source"], "encrypted_snapshot": True,
                           "self_wxid": None}
                self.channels[state] = binding
                return json.dumps(binding)
            if action == "status":
                return json.dumps({"binding": self.channels[state], "latest_cursor": len(self.events.get(state, []))})
            if action == "sync":
                self.channels[state]["self_wxid"] = "wxid_self"
                self.synced.append(state)
                return json.dumps({"events_appended": 0})
            if action == "events":
                after = int(argv[argv.index("--after") + 1])
                count = int(argv[argv.index("--limit") + 1])
                page = [item for item in self.events.get(state, []) if item["cursor"] > after][:count]
                return "\n".join(json.dumps(item) for item in page)
        box = self.outboxes.setdefault(state, {})
        if action == "list":
            return json.dumps([{"request_id": key, "status": item["status"]} for key, item in box.items()])
        rid = argv[argv.index("--request-id") + 1]
        if action == "queue":
            box.setdefault(rid, {"request_id": rid, "text": data, "status": "queued"})
            if self.on_queue is not None:
                self.on_queue()
            return json.dumps(box[rid])
        if action == "show":
            return json.dumps(box[rid])
        if action == "send":
            self.sent.append((state, rid))
            box[rid]["status"] = "ui_observed"
            return json.dumps(box[rid])
        raise AssertionError(argv)


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.registry = root / "registry"
        self.registry.mkdir(mode=0o700)
        source = root / "account"
        source.mkdir(mode=0o700)
        wxcli = root / "wx-cli"
        sender = root / "sender"
        for path in (wxcli, sender):
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o700)
        template = root / "sender-template.json"
        write_private(template, {"protocol_version": 1, "ui_dir": str(root / "ui")})
        self.settings = {"version": 1, "wx_cli": str(wxcli), "registry_dir": str(self.registry),
                         "account": "account", "source": str(source), "sender_template": str(template),
                         "sender_executable": str(sender), "controller_configs": []}
        self.fake = FakeCLI(self.settings)
        self.patch = mock.patch.object(conv.runner, "command", side_effect=self.fake)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def choose(self, index=0):
        id = self.fake.metadata[index]["conversation_id"]
        return conv.select(self.settings, id)

    def test_distinct_group_and_private_channels_and_read_without_ack(self):
        selected = [self.choose(index) for index in range(3)]
        self.assertEqual(len({item["state_dir"] for item in selected}), 3)
        self.assertEqual(self.fake.synced, [])
        result = conv.read(self.settings)
        self.assertEqual(result["conversation"]["kind"], "private")
        self.assertEqual(result["events"], [])
        self.assertEqual(len(self.fake.synced), 1)
        self.assertFalse(any("ack" in str(call) for call in self.fake.sent))

    def test_existing_controller_state_is_reused(self):
        metadata = self.fake.metadata[0]
        state = self.registry / "existing-controller"
        state.mkdir(mode=0o700)
        binding = {"channel_id": "existing-channel", "session_id": "existing-session",
                   "account": self.settings["account"], "talker": metadata["talker"],
                   "source": self.settings["source"], "encrypted_snapshot": True,
                   "self_wxid": "wxid_self"}
        self.fake.channels[str(state)] = binding
        controller_path = self.registry / "controller.json"
        controller = {"state_dir": str(state)}
        self.settings["controller_configs"] = [str(controller_path)]
        with mock.patch.object(conv, "controllers", return_value=[(controller_path, controller, binding)]), \
             mock.patch.object(conv.ctl, "config", return_value=controller):
            result = conv.select(self.settings, metadata["conversation_id"])
            self.assertTrue(result["imported_controller"])
            self.assertEqual(result["state_dir"], str(state))
            self.assertEqual(conv.selected(self.settings)[1]["channel_id"], "existing-channel")
        self.assertFalse((self.registry / "channels" / metadata["conversation_id"]).exists())

    def test_rename_and_ambiguous_name_refuse_desktop_send(self):
        self.choose()
        self.fake.metadata[0]["name"] = "Renamed"
        with self.assertRaisesRegex(conv.ConversationError, "select it again"):
            conv.send(self.settings, "first", "hello")
        self.assertEqual(self.fake.sent, [])
        self.choose()
        self.fake.metadata[1]["name"] = "Renamed"
        with self.assertRaisesRegex(conv.ConversationError, "may identify another"):
            conv.send(self.settings, "first", "hello")
        self.assertEqual(self.fake.sent, [])

    def test_native_title_suffix_collision_is_rejected_across_kinds(self):
        self.fake.metadata[0]["name"] = "Alice"
        self.fake.metadata[2]["name"] = "Alice (2)"
        self.assertEqual(len(conv.find(self.settings, name="Alice")["matches"]), 1)
        self.assertEqual(len(conv.find(self.settings, name="Alice", all_matches=True)["matches"]), 2)
        self.choose()
        with self.assertRaisesRegex(conv.ConversationError, "may identify another"):
            conv.send(self.settings, "hello", "hello")
        self.assertEqual(self.fake.sent, [])
        self.choose(2)
        with self.assertRaisesRegex(conv.ConversationError, "may identify another"):
            conv.send(self.settings, "private", "hello")
        self.assertEqual(self.fake.sent, [])
        self.choose()
        self.fake.metadata[2]["name"] = "ALICE（２）"
        with self.assertRaisesRegex(conv.ConversationError, "may identify another"):
            conv.focus_conversation(self.settings)
        self.assertEqual(self.fake.synced, [])

    def test_send_idempotent_conflict_and_unknown_does_not_retry(self):
        self.choose(2)
        first = conv.send(self.settings, "same", "hello")
        self.assertEqual(first["status"], "ui_observed")
        entry = conv.registry(self.settings)["entries"][self.fake.metadata[2]["conversation_id"]]
        config_path, driver_path = conv._driver_paths(entry)
        self.assertEqual(driver_path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        generated = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual((generated["target_name"], generated["conversation_kind"]), ("Friend", "private"))
        self.assertEqual(generated["binding"]["talker"], "wxid_friend")
        again = conv.send(self.settings, "same", "hello")
        self.assertTrue(again["already_completed"])
        self.assertEqual(len(self.fake.sent), 1)
        with self.assertRaisesRegex(conv.ConversationError, "different text"):
            conv.send(self.settings, "same", "changed")
        state = conv.registry(self.settings)["entries"][self.fake.metadata[2]["conversation_id"]]["state_dir"]
        self.fake.outboxes[state]["same"]["status"] = "indeterminate"
        with self.assertRaisesRegex(conv.ConversationError, "unresolved"):
            conv.send(self.settings, "same", "hello")
        self.assertEqual(len(self.fake.sent), 1)

    def test_selection_change_after_queue_refuses_desktop_effect(self):
        self.choose()
        def change_selection():
            value = conv.registry(self.settings)
            value["selected_id"] = None
            conv.save_registry(self.settings, value)
        self.fake.on_queue = change_selection
        with self.assertRaisesRegex(conv.ConversationError, "selection changed"):
            conv.send(self.settings, "race", "hello")
        self.assertEqual(self.fake.sent, [])

    def test_registry_cannot_redirect_channel_state(self):
        self.choose()
        id = self.fake.metadata[0]["conversation_id"]
        value = conv.registry(self.settings)
        value["entries"][id]["state_dir"] = str(self.registry / "other")
        conv.save_registry(self.settings, value)
        with self.assertRaisesRegex(conv.ConversationError, "manual channel path changed"):
            conv.read(self.settings)

    def test_reply_requires_incoming_source_and_uses_stable_id(self):
        self.choose(1)
        entry = conv.registry(self.settings)["entries"][self.fake.metadata[1]["conversation_id"]]
        self.fake.events[entry["state_dir"]] = [{"cursor": 1, "channel_id": entry["channel_id"],
            "session_id": entry["session_id"], "account": self.settings["account"],
            "talker": entry["talker"], "kind": "message_observed", "direction": "incoming",
            "server_id": 7, "message": {"talker": entry["talker"], "server_id": 7,
                                      "content": {"Text": "hello"}}}]
        result = conv.reply(self.settings, 1, "reply")
        self.assertEqual(result["request_id"], conv.runner.reply_request_id(entry["channel_id"], 7))
        self.assertEqual(result["cursor"], 1)
        self.assertTrue(conv.reply(self.settings, 1, "reply")["already_completed"])
        self.fake.events[entry["state_dir"]][0]["direction"] = "outgoing"
        with self.assertRaisesRegex(conv.ConversationError, "not an incoming"):
            conv.reply(self.settings, 1, "different")

    def test_controller_takeover_restores_only_acquired_lease(self):
        config_one = {"state_dir": str(self.registry / "controller-one")}
        config_two = {"state_dir": str(self.registry / "controller-two")}
        for value in (config_one, config_two):
            Path(value["state_dir"]).mkdir(mode=0o700)
        configured = [(Path("/one"), config_one, {"talker": "one@chatroom"}),
                      (Path("/two"), config_two, {"talker": "two@chatroom"})]
        calls = []
        with mock.patch.object(conv, "controllers", return_value=configured), \
             mock.patch.object(conv.ctl, "status", side_effect=[{"alive": True}, {"alive": False}]), \
             mock.patch.object(conv.interactive, "select", side_effect=lambda value, talker: calls.append(("select", talker))), \
             mock.patch.object(conv.interactive, "_status", return_value={"binding": {}, "consumer_cursor": 0}), \
             mock.patch.object(conv.interactive, "_runner_state", return_value=(None, {"pending": None}, 0)), \
             mock.patch.object(conv.interactive, "_check_outbox_clear"), \
             mock.patch.object(conv.interactive, "takeover", side_effect=lambda value: calls.append(("take", value["state_dir"]))), \
             mock.patch.object(conv.interactive, "handback", side_effect=lambda value: calls.append(("back", value["state_dir"]))):
            with conv.coordinated(self.settings, {"state_dir": "manual", "controller_config": None}):
                pass
        self.assertEqual([item[0] for item in calls], ["select", "take", "back"])
        lease = Path(config_two["state_dir"]) / conv.ctl.INTERACTIVE_NAME
        write_private(lease, {"version": 1})
        with mock.patch.object(conv, "controllers", return_value=configured):
            with self.assertRaisesRegex(conv.ConversationError, "another operator"):
                with conv.coordinated(self.settings, {"state_dir": "manual", "controller_config": None}):
                    pass

    def test_handback_failure_is_reported_and_not_retried(self):
        value = {"state_dir": str(self.registry / "controller")}
        Path(value["state_dir"]).mkdir(mode=0o700)
        configured = [(Path("/controller"), value, {"talker": "one@chatroom"})]
        with mock.patch.object(conv, "controllers", return_value=configured), \
             mock.patch.object(conv.ctl, "status", return_value={"alive": True}), \
             mock.patch.object(conv.interactive, "select"), \
             mock.patch.object(conv.interactive, "_status", return_value={"binding": {}, "consumer_cursor": 0}), \
             mock.patch.object(conv.interactive, "_runner_state", return_value=(None, {"pending": None}, 0)), \
             mock.patch.object(conv.interactive, "_check_outbox_clear"), \
             mock.patch.object(conv.interactive, "takeover"), \
             mock.patch.object(conv.interactive, "handback", side_effect=conv.interactive.ChatError("pending", "pending send")) as back:
            with self.assertRaisesRegex(conv.ConversationError, "lease retained"):
                with conv.coordinated(self.settings, {"state_dir": "manual", "controller_config": None}):
                    pass
        back.assert_called_once()

    def test_config_private_permissions(self):
        path = self.registry / "config.json"
        write_private(path, self.settings)
        self.assertEqual(conv.config(path)["account"], "account")
        path.chmod(0o644)
        with self.assertRaises(conv.ctl.ControlError):
            conv.config(path)


if __name__ == "__main__":
    unittest.main()
