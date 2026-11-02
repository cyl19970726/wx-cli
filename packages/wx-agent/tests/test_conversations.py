"""Synthetic conversation registry and guarded outbox tests."""
import base64
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
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
        self.recoveries = []
        self.on_queue = None
        self.calls = []
        self.managed_states = set()

    def __call__(self, argv, data=None, timeout=60, limit=None):
        self.calls.append(list(argv))
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
        if action == "queue-image":
            media = conv.sender.image_descriptor(argv[argv.index("--image") + 1])
            if media["sha256"] != argv[argv.index("--expected-sha256") + 1]:
                raise conv.runner.RunnerError("source changed before queue")
            box.setdefault(rid, {"request_id": rid, "text": "", "media": media,
                                 "status": "managed_media_queued" if state in self.managed_states else "media_queued",
                                 **({key: int(argv[argv.index(flag) + 1]) if key in ("part_index", "priority") else
                                     argv[argv.index(flag) + 1] for key, flag in
                                    (("batch_id", "--batch-id"), ("part_index", "--part-index"),
                                     ("priority", "--priority"), ("expires_at", "--expires-at")) if flag in argv}),
                                 **({"managed_owner": argv[argv.index("--owner") + 1],
                                     "managed_epoch": int(argv[argv.index("--expected-epoch") + 1])}
                                    if state in self.managed_states else {})})
            if self.on_queue is not None:
                self.on_queue()
            return json.dumps(box[rid])
        if action == "queue":
            box.setdefault(rid, {"request_id": rid, "text": data,
                                 "status": "managed_queued" if state in self.managed_states else "queued",
                                 **({key: int(argv[argv.index(flag) + 1]) if key in ("part_index", "priority") else
                                     argv[argv.index(flag) + 1] for key, flag in
                                    (("batch_id", "--batch-id"), ("part_index", "--part-index"),
                                     ("priority", "--priority"), ("expires_at", "--expires-at")) if flag in argv}),
                                 **({"managed_owner": argv[argv.index("--owner") + 1],
                                     "managed_epoch": int(argv[argv.index("--expected-epoch") + 1])}
                                    if state in self.managed_states else {})})
            if self.on_queue is not None:
                self.on_queue()
            return json.dumps(box[rid])
        if action == "show":
            return json.dumps(box[rid])
        if action == "send":
            self.sent.append((state, rid))
            box[rid]["status"] = "ui_observed"
            box[rid]["attempt_number"] = 1
            return json.dumps(box[rid])
        if action == "recover-unsent":
            expected = int(argv[argv.index("--expected-attempt") + 1])
            self.recoveries.append((state, rid, expected, argv[argv.index("--driver") + 1]))
            box[rid]["attempt_number"] = expected + 1
            box[rid]["status"] = "ui_observed"
            return json.dumps({"request_id": rid, "attempt_number": expected + 1,
                               "status": "ui_observed"})
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

    def make_image(self, name="中文 截图.png"):
        path = self.registry / name
        path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
        return path

    def test_unenrolled_gui_refuses_before_intent_when_desktop_is_managed(self):
        self.choose(0)
        desktop=Path(json.loads(Path(self.settings["sender_template"]).read_text())["ui_dir"])
        desktop.mkdir(mode=0o700)
        (desktop/"coordinator.sqlite3").write_bytes(b"synthetic managed desktop marker")
        with self.assertRaisesRegex(conv.ConversationError,"desktop is managed"):
            conv.send(self.settings,"must-not-freeze","synthetic request")
        self.assertEqual(self.fake.sent,[])
        self.assertFalse(any("must-not-freeze" in box for box in self.fake.outboxes.values()))

    def test_prepare_driver_supports_candidate_python_interpreter(self):
        state=self.registry/"channel"
        state.mkdir(mode=0o700)
        python=self.registry/"venv/bin/python3"
        python.parent.mkdir(parents=True,mode=0o700)
        python.write_text("#!/bin/sh\n")
        python.chmod(0o700)
        module=self.registry/"venv/lib/python3.9/site-packages/wx_agent/macos_sender_driver.py"
        module.parent.mkdir(parents=True,mode=0o700)
        module.write_text("synthetic installed sender")
        settings=dict(self.settings,sender_executable=str(python))
        with mock.patch.object(conv,"channel_status",return_value={"binding":{}}):
            conv.prepare_driver(settings,{"state_dir":str(state),"name":"Synthetic Group","kind":"group"})
        self.assertIn("-m wx_agent.macos_sender_driver",(state/"conversation-sender").read_text())

    def test_candidate_cli_uses_only_stopped_enrolled_controller_without_rewriting_config(self):
        legacy={"wx_cli":"/legacy/bin/wx-cli","state_dir":str(self.registry/"legacy")}
        original=legacy.copy();root=self.registry/"ui"
        with mock.patch.object(conv.dispatch,"managed_root",return_value=root), \
             mock.patch.object(conv.dispatch,"root_from_settings",return_value=root), \
             mock.patch.object(conv.ctl,"status",return_value={"alive":False}) as status:
            selected=conv.controller_api(self.settings,legacy)
            self.assertEqual(selected["wx_cli"],self.settings["wx_cli"])
            self.assertEqual(legacy,original)
            status.assert_called_once_with(legacy)
            status.return_value={"alive":True}
            with self.assertRaisesRegex(conv.ConversationError,"stop the original"):
                conv.controller_api(self.settings,legacy)
        with mock.patch.object(conv.dispatch,"managed_root",return_value=None):
            with self.assertRaisesRegex(conv.ConversationError,"outside managed migration"):
                conv.controller_api(self.settings,legacy)

    def managed_binding(self, entry, owner="session-a", epoch=4):
        return {"channel_state_dir": entry["state_dir"], "channel_id": entry["channel_id"],
                "session_id": entry["session_id"], "owner": owner, "epoch": epoch,
                "control": "active"}

    def test_image_send_identity_conflict_and_restart_never_repeats(self):
        for kind in (0, 2):
            self.choose(kind)
            path = self.make_image()
            rid = "image-" + str(kind)
            result = conv.send_image(self.settings, rid, path)
            self.assertEqual(result["status"], "ui_observed")
            self.assertTrue(conv.send_image(self.settings, rid, path)["already_completed"])
            self.assertEqual(sum(r == rid for _, r in self.fake.sent), 1)
            path.write_bytes(path.read_bytes() + b"changed")
            with self.assertRaisesRegex(conv.ConversationError, "different image"):
                conv.send_image(self.settings, rid, path)
            state = self.fake.sent[-1][0]
            self.fake.outboxes[state][rid]["status"] = "indeterminate"
            path = self.make_image()
            with self.assertRaisesRegex(conv.ConversationError, "unresolved"):
                conv.send_image(self.settings, rid, path)
            self.assertEqual(sum(r == rid for _, r in self.fake.sent), 1)

    def test_image_validation_precedes_selection_and_no_path_as_text(self):
        path = self.registry / "bad.gif"
        for data in (b"", b"GIF89a", b"plain path"):
            path.write_bytes(data)
            with self.assertRaises(conv.sender.Refused):
                conv.send_image(self.settings, "bad-image", path)
        self.assertEqual(self.fake.calls, [])
        self.choose()
        path = self.make_image()
        conv.send_image(self.settings, "image-good", path)
        self.assertEqual(self.fake.outboxes[self.fake.sent[-1][0]]["image-good"]["text"], "")
        with self.assertRaisesRegex(conv.ConversationError, "different text"):
            conv.send(self.settings, "image-good", str(path))

    def test_image_changes_between_descriptor_and_queue_creates_no_intent(self):
        self.choose()
        path = self.make_image()
        original = self.fake.__call__
        def changed(argv, **kwargs):
            if "queue-image" in argv:
                path.write_bytes(path.read_bytes() + b"changed")
            return original(argv, **kwargs)
        with mock.patch.object(conv.runner, "command", side_effect=changed), self.assertRaises(conv.ConversationError):
            conv.send_image(self.settings, "race-image", path)
        self.assertFalse(any("race-image" in box for box in self.fake.outboxes.values()))
        self.assertEqual(self.fake.sent, [])

    def test_image_selection_drift_during_queue_stops_before_send(self):
        self.choose()
        path = self.make_image()
        def drift():
            registry = conv.registry(self.settings)
            registry["selected_id"] = "invalid"
            conv.save_registry(self.settings, registry)
        self.fake.on_queue = drift
        with self.assertRaises(conv.ConversationError):
            conv.send_image(self.settings, "image-drift", path)
        self.assertEqual(self.fake.sent, [])

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

    def test_stdin_terminal_newline_refused_before_queue_and_multiline_stays_exact(self):
        self.choose(2)
        state = conv.registry(self.settings)["entries"][self.fake.metadata[2]["conversation_id"]]["state_dir"]

        def invoke(raw):
            stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(conv.sys, "stdin", stdin), \
                 mock.patch.object(conv, "config", return_value=self.settings), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = conv.main(["--config", "/synthetic/config.json", "send",
                                  "--request-id", "same-multiline", "--text-stdin"])
            return code, stdout.getvalue(), stderr.getvalue()

        for raw in ("你好 😊\n第二行\n".encode(), "你好 😊\n第二行\r\n".encode()):
            with mock.patch.object(conv, "coordinated", side_effect=AssertionError("invalid text reached coordination")):
                code, _, stderr = invoke(raw)
            self.assertEqual(code, 1)
            self.assertIn("trailing line break", stderr)
            self.assertNotIn("same-multiline", self.fake.outboxes.get(state, {}))
            self.assertEqual(self.fake.sent, [])

        exact = "你好 😊\n第二行".encode()
        code, stdout, _ = invoke(exact)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "ui_observed")
        self.assertEqual(self.fake.outboxes[state]["same-multiline"]["text"], exact.decode())
        code, stdout, _ = invoke(exact)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["already_completed"])
        self.assertEqual(len(self.fake.sent), 1)
        code, _, stderr = invoke("你好 😊\n不同正文".encode())
        self.assertEqual(code, 1)
        self.assertIn("different text", stderr)
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

    def test_explicit_id_survives_default_selection_change_but_rejects_entry_revision(self):
        self.choose(0)
        target = self.fake.metadata[0]["conversation_id"]
        self.choose(2)
        self.fake.on_queue = lambda: self.choose(1)
        result = conv.send(self.settings, "explicit", "hello", id=target)
        self.assertEqual(result["status"], "ui_observed")
        self.assertEqual(self.fake.sent[-1][0], conv.registry(self.settings)["entries"][target]["state_dir"])
        self.fake.on_queue = None
        snapshot, entry = conv.selected(self.settings, target)
        current = conv.registry(self.settings)
        current["entries"][target]["revision"] += 1
        conv.save_registry(self.settings, current)
        with self.assertRaisesRegex(conv.ConversationError, "selection changed"):
            conv.assert_selection(self.settings, snapshot, entry)

    def test_read_other_channel_does_not_wait_for_blocked_sync(self):
        self.choose(0)
        self.choose(2)
        group_id = self.fake.metadata[0]["conversation_id"]
        private_id = self.fake.metadata[2]["conversation_id"]
        group_state = conv.registry(self.settings)["entries"][group_id]["state_dir"]
        entered = threading.Event()
        release = threading.Event()
        original = self.fake.__call__
        errors = []

        def blocked(argv, **kwargs):
            if argv[1] == "channel" and "sync" in argv and group_state in argv:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("blocked sync was not released")
            return original(argv, **kwargs)

        def read_group():
            try:
                conv.read(self.settings, id=group_id)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(conv.runner, "command", side_effect=blocked):
            thread = threading.Thread(target=read_group)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(conv.read(self.settings, id=private_id)["conversation"]["conversation_id"],
                                 private_id)
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_same_channel_sync_is_serialized(self):
        self.choose()
        target = self.fake.metadata[0]["conversation_id"]
        entered = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        original = self.fake.__call__
        sync_calls = []
        errors = []

        def blocked(argv, **kwargs):
            if argv[1] == "channel" and "sync" in argv:
                sync_calls.append(threading.current_thread().name)
                if len(sync_calls) == 1:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("blocked sync was not released")
            return original(argv, **kwargs)

        def read_target(done=None):
            try:
                conv.read(self.settings, id=target)
            except BaseException as exc:
                errors.append(exc)
            finally:
                if done is not None:
                    done.set()

        with mock.patch.object(conv.runner, "command", side_effect=blocked):
            first = threading.Thread(target=read_target, name="first")
            second = threading.Thread(target=read_target, args=(second_done,), name="second")
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
                self.assertFalse(second_done.wait(0.05))
                self.assertEqual(sync_calls, ["first"])
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertEqual(sync_calls, ["first", "second"])
        self.assertEqual(errors, [])

    def test_configure_and_control_require_explicit_id_and_epoch(self):
        self.choose(2)
        entry = conv.selected(self.settings)[1]
        target = entry["conversation_id"]
        root = self.registry / "ui"
        root.mkdir(mode=0o700)
        with mock.patch.object(conv.dispatch, "configure", return_value={"epoch": 0}) as configured:
            self.assertEqual(conv.configure(self.settings, target, "session-a"), {"epoch": 0})
            configured.assert_called_once_with(self.settings, entry, "session-a", "pull", "observe", None)
        with mock.patch.object(conv, "_managed_root", return_value=root), \
             mock.patch.object(conv.dispatch, "call", return_value={"epoch": 2}) as called:
            self.assertEqual(conv.control(self.settings, target, "takeover", 1), {"epoch": 2})
            called.assert_called_once_with(self.settings["wx_cli"], root, "takeover",
                                           "--conversation-id", target, "--expected-epoch", 1)
        with self.assertRaises(conv.ConversationError):
            conv.control(self.settings, target, "handback", -1)
        with mock.patch.object(conv, "config", return_value=self.settings), \
             mock.patch.object(conv, "configure", return_value={"epoch": 1}) as configure_cli, \
             mock.patch.object(conv, "control", return_value={"epoch": 2}) as control_cli, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(conv.main(["--config", "/synthetic/config.json", "configure", "--id", target,
                                        "--owner", "session-a"]), 0)
            self.assertEqual(conv.main(["--config", "/synthetic/config.json", "takeover", "--id", target,
                                        "--expected-epoch", "1"]), 0)
            self.assertEqual(conv.main(["--config", "/synthetic/config.json", "handback", "--id", target,
                                        "--expected-epoch", "2"]), 0)
        configure_cli.assert_called_once_with(self.settings, target, "session-a", "pull", "observe", None)
        self.assertEqual(control_cli.call_args_list, [mock.call(self.settings, target, "takeover", 1),
                                                       mock.call(self.settings, target, "handback", 2)])
        for action in ("configure", "takeover", "handback"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                conv.main(["--config", "/synthetic/config.json", action])

    def test_managed_send_returns_pending_and_never_uses_raw_outbox_send(self):
        self.choose(2)
        entry = conv.selected(self.settings)[1]
        state = entry["state_dir"]
        self.fake.managed_states.add(state)
        root = self.registry / "ui"
        with mock.patch.object(conv.dispatch, "managed_root", return_value=root), \
             mock.patch.object(conv.dispatch, "root_from_settings", return_value=root), \
             mock.patch.object(conv.dispatch, "call", return_value=self.managed_binding(entry)), \
             mock.patch.object(conv.dispatch, "send_existing",
                               return_value={"request_id": "managed", "status": "pending", "dispatch_id": 7}) as send:
            result = conv.send(self.settings, "managed", "hello", id=entry["conversation_id"],
                               owner="session-a", expected_epoch=4)
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["dispatch_id"], 7)
            self.assertEqual(conv.send(self.settings, "managed", "hello", id=entry["conversation_id"],
                                       owner="session-a", expected_epoch=4)["status"],
                             "pending")
            self.assertEqual(send.call_count, 2)
        self.assertEqual(self.fake.sent, [])
        self.assertFalse(any("send" in call for call in self.fake.calls if "outbox" in call))

    def test_managed_no_wait_freezes_new_text_and_image_before_enqueue(self):
        self.choose(2)
        entry = conv.selected(self.settings)[1]
        state = entry["state_dir"]
        self.fake.managed_states.add(state)
        image = self.make_image()
        root = self.registry / "ui"
        scheduling = {"batch_id": "batch-a", "part_index": 1, "priority": 2}

        def enqueue(_, channel_state, request_id, owner, expected_epoch):
            self.assertEqual(channel_state, state)
            self.assertEqual((owner, expected_epoch), ("session-a", 4))
            item = self.fake.outboxes[channel_state][request_id]
            config_path, driver_path = conv._driver_paths(entry)
            self.assertTrue(config_path.is_file())
            self.assertTrue(driver_path.is_file())
            self.assertEqual((item["managed_owner"], item["managed_epoch"]), (owner, expected_epoch))
            self.assertEqual(item["status"], "managed_media_queued" if request_id == "image" else "managed_queued")
            self.assertEqual(item["batch_id"], "batch-a")
            self.assertEqual(item["part_index"], 1)
            return item, {"dispatch_id": 17}

        with mock.patch.object(conv.dispatch, "managed_root", return_value=root), \
             mock.patch.object(conv.dispatch, "root_from_settings", return_value=root), \
             mock.patch.object(conv.dispatch, "call", return_value=self.managed_binding(entry)), \
             mock.patch.object(conv.dispatch, "enqueue_existing", side_effect=enqueue) as queued, \
             mock.patch.object(conv.dispatch, "send_existing") as send:
            text = conv.send(self.settings, "text", "hello", id=entry["conversation_id"],
                             owner="session-a", expected_epoch=4, scheduling=scheduling, wait=False)
            picture = conv.send_image(self.settings, "image", image, id=entry["conversation_id"],
                                      owner="session-a", expected_epoch=4, scheduling=scheduling, wait=False)
        self.assertEqual((text["status"], picture["status"]), ("pending", "pending"))
        self.assertEqual(queued.call_count, 2)
        send.assert_not_called()
        self.assertEqual(self.fake.sent, [])
        self.assertTrue(any(call[1] == "outbox" and "queue" in call for call in self.fake.calls))
        self.assertTrue(any(call[1] == "outbox" and "queue-image" in call for call in self.fake.calls))

    def test_managed_no_wait_reused_conflicts_and_unknown_never_enqueue(self):
        self.choose(2)
        entry = conv.selected(self.settings)[1]
        state = entry["state_dir"]
        self.fake.managed_states.add(state)
        self.fake.outboxes[state] = {"same": {"request_id": "same", "text": "original",
            "status": "managed_queued", "managed_owner": "session-a", "managed_epoch": 4,
            "batch_id": "batch-a", "part_index": 1}}
        image = self.make_image()
        self.fake.outboxes[state]["photo"] = {"request_id": "photo", "text": "",
            "media": conv.sender.image_descriptor(image), "status": "managed_media_queued",
            "managed_owner": "session-a", "managed_epoch": 4}
        image.write_bytes(image.read_bytes() + b"changed")
        root = self.registry / "ui"
        kwargs = dict(id=entry["conversation_id"], owner="session-a", expected_epoch=4, wait=False)
        with mock.patch.object(conv.dispatch, "managed_root", return_value=root), \
             mock.patch.object(conv.dispatch, "root_from_settings", return_value=root), \
             mock.patch.object(conv.dispatch, "call", return_value=self.managed_binding(entry)), \
             mock.patch.object(conv.dispatch, "enqueue_existing") as queued:
            with self.assertRaisesRegex(conv.ConversationError, "different text"):
                conv.send(self.settings, "same", "changed", **kwargs)
            with self.assertRaisesRegex(conv.ConversationError, "different batch"):
                conv.send(self.settings, "same", "original", scheduling={"batch_id": "batch-b"}, **kwargs)
            with self.assertRaisesRegex(conv.ConversationError, "different image"):
                conv.send_image(self.settings, "photo", image, **kwargs)
            self.fake.outboxes[state]["same"]["status"] = "indeterminate"
            with self.assertRaisesRegex(conv.ConversationError, "unresolved"):
                conv.send(self.settings, "same", "original", **kwargs)
            queued.assert_not_called()

    def test_managed_send_refuses_missing_or_stale_owner_before_freeze(self):
        self.choose(2)
        entry = conv.selected(self.settings)[1]
        state = entry["state_dir"]
        self.fake.managed_states.add(state)
        root = self.registry / "ui"
        with mock.patch.object(conv.dispatch, "managed_root", return_value=root), \
             mock.patch.object(conv.dispatch, "root_from_settings", return_value=root), \
             mock.patch.object(conv.dispatch, "call", return_value=self.managed_binding(entry,
                                                                                         owner="new-owner", epoch=8)), \
             mock.patch.object(conv.dispatch, "send_existing") as send:
            for owner, epoch in ((None, None), ("old-owner", 7), ("new-owner", 7)):
                with self.assertRaises(conv.ConversationError):
                    conv.send(self.settings, "stale", "hello", id=entry["conversation_id"],
                              owner=owner, expected_epoch=epoch)
            self.assertNotIn("stale", self.fake.outboxes.get(state, {}))
            send.assert_not_called()

    def test_managed_orphan_intent_is_not_adopted_by_current_owner(self):
        self.choose(2)
        entry = conv.selected(self.settings)[1]
        state = entry["state_dir"]
        self.fake.managed_states.add(state)
        self.fake.outboxes[state] = {"orphan": {"request_id": "orphan", "text": "hello",
                                                "status": "managed_queued"}}
        root = self.registry / "ui"
        with mock.patch.object(conv.dispatch, "managed_root", return_value=root), \
             mock.patch.object(conv.dispatch, "root_from_settings", return_value=root), \
             mock.patch.object(conv.dispatch, "call", return_value=self.managed_binding(entry)), \
             mock.patch.object(conv.dispatch, "send_existing") as send:
            with self.assertRaisesRegex(conv.ConversationError, "originated under another owner epoch"):
                conv.send(self.settings, "orphan", "hello", id=entry["conversation_id"],
                          owner="session-a", expected_epoch=4)
            send.assert_not_called()

    def test_managed_send_flags_reach_text_image_and_reply_api(self):
        target = self.fake.metadata[0]["conversation_id"]
        input_text = io.TextIOWrapper(io.BytesIO(b"hello"), encoding="utf-8")
        with mock.patch.object(conv, "config", return_value=self.settings), \
             mock.patch.object(conv.sys, "stdin", input_text), \
             mock.patch.object(conv, "send", return_value={"status": "pending"}) as sending, \
             mock.patch.object(conv, "send_image", return_value={"status": "pending"}) as image_sending, \
             mock.patch.object(conv, "reply", return_value={"status": "pending"}) as replying, \
             contextlib.redirect_stdout(io.StringIO()):
            common = ["--id", target, "--owner", "session-a", "--expected-epoch", "4"]
            self.assertEqual(conv.main(["--config", "/synthetic/config.json", "send", *common,
                                        "--request-id", "rid", "--text-stdin"]), 0)
            self.assertEqual(conv.main(["--config", "/synthetic/config.json", "send-image", *common,
                                        "--request-id", "image-rid", "--image", "/synthetic/image.png"]), 0)
            self.assertEqual(conv.main(["--config", "/synthetic/config.json", "reply", *common,
                                        "--cursor", "3", "--text-stdin"]), 0)
        sending.assert_called_once_with(self.settings, "rid", "hello", target,
                                        owner="session-a", expected_epoch=4, wait=True, scheduling={})
        image_sending.assert_called_once_with(self.settings, "image-rid", Path("/synthetic/image.png"), target,
                                              owner="session-a", expected_epoch=4, wait=True, scheduling={})
        replying.assert_called_once_with(self.settings, 3, "", target,
                                         owner="session-a", expected_epoch=4)

    def test_managed_image_uses_same_dispatch_seam(self):
        self.choose(2)
        entry = conv.selected(self.settings)[1]
        state = entry["state_dir"]
        self.fake.managed_states.add(state)
        image = self.make_image()
        root = self.registry / "ui"

        def complete(_, channel_state, request_id, **kwargs):
            item = self.fake.outboxes[channel_state][request_id]
            item["status"] = "ui_observed"
            return item

        with mock.patch.object(conv.dispatch, "managed_root", return_value=root), \
             mock.patch.object(conv.dispatch, "root_from_settings", return_value=root), \
             mock.patch.object(conv.dispatch, "call", return_value=self.managed_binding(entry)), \
             mock.patch.object(conv.dispatch, "send_existing", side_effect=complete) as sent:
            result = conv.send_image(self.settings, "managed-image", image, id=entry["conversation_id"],
                                     owner="session-a", expected_epoch=4)
        self.assertEqual(result["status"], "ui_observed")
        sent.assert_called_once_with(self.settings["wx_cli"], state, "managed-image",
                                     owner="session-a", expected_epoch=4)
        self.assertEqual(self.fake.sent, [])

    def test_managed_focus_is_queued_with_current_epoch(self):
        self.choose(0)
        entry = conv.selected(self.settings)[1]
        root = self.registry / "ui"
        calls = []

        def coordinator(binary, surface, *args):
            calls.append(args)
            if args[0] == "status":
                return {"channel_state_dir": entry["state_dir"], "channel_id": entry["channel_id"],
                        "session_id": entry["session_id"], "epoch": 4}
            if args[0] == "enqueue-focus":
                return {"dispatch_id": 9}
            return {"dispatches": [{"dispatch_id": 9, "status": "done", "result": "focus_completed"}]}

        with mock.patch.object(conv.dispatch, "managed_root", return_value=root), \
             mock.patch.object(conv.dispatch, "root_from_settings", return_value=root), \
             mock.patch.object(conv.dispatch, "call", side_effect=coordinator), \
             mock.patch.object(conv.dispatch, "drain"):
            self.assertTrue(conv.focus_conversation(self.settings, id=entry["conversation_id"])["focused"])
        self.assertEqual(calls[1][:3], ("enqueue-focus", "--conversation-id", entry["conversation_id"]))
        self.assertEqual(calls[1][-2:], ("--expected-epoch", 4))
        self.assertFalse(any("--focus-only" in call for call in self.fake.calls))

    def test_managed_imported_reply_pending_keeps_consumer_unacknowledged(self):
        self.choose()
        entry = conv.selected(self.settings)[1]
        entry.update(imported=True, controller_config="/synthetic/controller.json")
        state = Path(entry["state_dir"])
        write_private(state / conv.interactive.LEASE_NAME,
                      {"pending": {"cursor": 3, "text": "answer", "request_id": "existing-reply"}})
        self.fake.outboxes[str(state)] = {"existing-reply": {"request_id": "existing-reply",
            "text": "answer", "status": "managed_queued"}}
        with mock.patch.object(conv, "selected_channel", return_value=contextlib.nullcontext(({}, entry))), \
             mock.patch.object(conv, "fresh"), mock.patch.object(conv, "assert_selection"), \
             mock.patch.object(conv, "coordinated", return_value=contextlib.nullcontext()), \
             mock.patch.object(conv, "_managed_root", return_value=self.registry), \
             mock.patch.object(conv.dispatch, "call", return_value=self.managed_binding(entry)), \
             mock.patch.object(conv.ctl, "config", return_value={"state_dir": str(state),"wx_cli":self.settings["wx_cli"]}), \
             mock.patch.object(conv.interactive, "reply",
                               side_effect=conv.interactive.ChatError("send_uncertain", "queued")) as imported_reply:
            result = conv.reply(self.settings, 3, "answer", id=entry["conversation_id"],
                                owner="session-a", expected_epoch=4)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["request_id"], "existing-reply")
        passed_settings = imported_reply.call_args.args[0]
        self.assertEqual((passed_settings["_dispatch_owner"], passed_settings["_dispatch_epoch"]),
                         ("session-a", 4))
        self.assertFalse(any("ack" in call for call in self.fake.calls))

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

    def test_private_to_group_paginated_read_and_reply_preserves_failed_requests(self):
        entries = []
        for index in (2, 0):
            self.choose(index)
            entry = conv.selected(self.settings)[1]
            entries.append(entry)
            self.fake.events[entry["state_dir"]] = [
                {"cursor": cursor, "channel_id": entry["channel_id"], "session_id": entry["session_id"],
                 "account": self.settings["account"], "talker": entry["talker"],
                 "kind": "message_observed", "direction": "incoming", "server_id": 10 + cursor,
                 "message": {"talker": entry["talker"], "server_id": 10 + cursor,
                             "sender": "synthetic_peer", "content": {"Text": "question " + str(cursor)}}}
                for cursor in (1, 2)]
        private, group = entries
        failed = {"request_id": "historical-failed", "text": "previous independent intent",
                  "status": "indeterminate", "attempt_number": 1,
                  "attempts": [{"attempt_number": 1, "status": "indeterminate", "evidence": "retained"}]}
        state = group["state_dir"]
        self.fake.outboxes[state] = {failed["request_id"]: failed.copy()}
        original = json.dumps(self.fake.outboxes[state], sort_keys=True)
        # Model/runner coordination is not needed for a manual channel read or reply.
        with mock.patch.object(conv.interactive, "takeover", side_effect=AssertionError("runner started")), \
             mock.patch.object(conv.interactive, "handback", side_effect=AssertionError("runner started")):
            for entry in (private, group):
                conv.select(self.settings, entry["conversation_id"])
                self.assertTrue(conv.focus_conversation(self.settings)["focused"])
                first = conv.read(self.settings, limit=1)
                self.assertEqual(first["conversation"]["conversation_id"], entry["conversation_id"])
                self.assertTrue(first["has_more"])
                second = conv.read(self.settings, after=first["next_cursor"], limit=1)
                self.assertFalse(second["has_more"])
                self.assertEqual([first["events"][0]["cursor"], second["events"][0]["cursor"]], [1, 2])
                reply = conv.reply(self.settings, 2, "answer to " + entry["kind"])
                self.assertEqual(reply["request_id"], conv.runner.reply_request_id(entry["channel_id"], 12))
                self.assertTrue(conv.reply(self.settings, 2, "answer to " + entry["kind"])["already_completed"])
                self.assertEqual(conv.read(self.settings, after=1)["events"], second["events"])
        self.assertEqual(len(self.fake.sent), 2)
        self.assertNotEqual(self.fake.sent[0][1], self.fake.sent[1][1])
        self.assertEqual(self.fake.recoveries, [])
        self.assertFalse(any("ack" in call for call in self.fake.calls))
        self.assertEqual(json.dumps({failed["request_id"]: self.fake.outboxes[state][failed["request_id"]]},
                                    sort_keys=True), original)
        sent_before = list(self.fake.sent)
        with self.assertRaisesRegex(conv.ConversationError, "unresolved"):
            conv.send(self.settings, failed["request_id"], failed["text"])
        self.assertEqual(self.fake.sent, sent_before)
        self.assertEqual(conv.selected(self.settings)[1], group)

    def test_recover_unsent_send_requires_stable_conversation_and_reuses_exact_intent(self):
        self.choose(2)
        entry = conv.registry(self.settings)["entries"][self.fake.metadata[2]["conversation_id"]]
        state = entry["state_dir"]
        self.fake.outboxes[state] = {"original-request": {"request_id": "original-request",
            "text": "exact original text", "status": "indeterminate", "attempt_number": 1}}
        result = conv.recover_unsent_send(self.settings, "original-request", 1,
                                          entry["conversation_id"])
        self.assertEqual(result, {"request_id": "original-request", "attempt_number": 2,
                                  "status": "ui_observed", "already_completed": False})
        self.assertEqual(len(self.fake.recoveries), 1)
        self.assertEqual(self.fake.recoveries[0][:3], (state, "original-request", 1))
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(self.fake.outboxes[state]["original-request"]["text"], "exact original text")

    def test_recovery_rejects_terminal_or_stale_attempt_without_calling_rust(self):
        self.choose(2)
        entry = conv.registry(self.settings)["entries"][self.fake.metadata[2]["conversation_id"]]
        state = entry["state_dir"]
        self.fake.outboxes[state] = {"original-request": {"request_id": "original-request",
            "text": "original", "status": "ui_observed", "attempt_number": 1}}
        with self.assertRaisesRegex(conv.ConversationError, "failed or indeterminate"):
            conv.recover_unsent_send(self.settings, "original-request", 1, entry["conversation_id"])
        self.fake.outboxes[state]["original-request"].update(status="failed", attempt_number=2)
        with self.assertRaisesRegex(conv.ConversationError, "differs from the requested recovery"):
            conv.recover_unsent_send(self.settings, "original-request", 1, entry["conversation_id"])
        self.assertEqual(self.fake.recoveries, [])

    def test_recovery_cli_requires_stable_conversation_id(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as missing:
            conv.main(["--config", "/synthetic/config.json", "recover-unsent-send",
                       "--request-id", "original-request", "--expected-attempt", "1"])
        self.assertEqual(missing.exception.code, 2)
        self.assertIn("--id", errors.getvalue())
        self.assertIn("required", errors.getvalue())

    def test_imported_controller_recovery_uses_retained_lease_without_coordinator(self):
        metadata = self.fake.metadata[0]
        state = self.registry / "imported-controller"
        state.mkdir(mode=0o700)
        binding = {"channel_id": "imported-channel", "session_id": "imported-session",
                   "account": self.settings["account"], "talker": metadata["talker"],
                   "source": self.settings["source"], "encrypted_snapshot": True,
                   "self_wxid": "wxid_self"}
        self.fake.channels[str(state)] = binding
        config_path = self.registry / "imported-controller.json"
        controller = {"state_dir": str(state), "wx_cli": self.settings["wx_cli"]}
        self.settings["controller_configs"] = [str(config_path)]
        with mock.patch.object(conv, "controllers", return_value=[(config_path, controller, binding)]), \
             mock.patch.object(conv.ctl, "config", return_value=controller):
            conv.select(self.settings, metadata["conversation_id"])
            entry = conv.registry(self.settings)["entries"][metadata["conversation_id"]]
            request_id = "stable-reply"
            lease = {"version": 1, "phase": "active", "pending": {
                "cursor": 8, "request_id": request_id, "text": "same text"}}
            write_private(state / conv.interactive.LEASE_NAME, lease)
            self.fake.outboxes[str(state)] = {request_id: {"request_id": request_id, "text": "same text",
                "status": "failed", "attempt_number": 1}}
            with mock.patch.object(conv, "coordinated", side_effect=AssertionError("retakeover")), \
                 mock.patch.object(conv.interactive, "recover_unsent_reply",
                                   return_value={"request_id": request_id, "status": "ui_observed"}) as recover:
                result = conv.recover_unsent_send(self.settings, request_id, 1, entry["conversation_id"])
        self.assertEqual(result["request_id"], request_id)
        recover.assert_called_once_with(controller, 8, 1)

    def test_controller_takeover_restores_only_acquired_lease(self):
        config_one = {"state_dir": str(self.registry / "controller-one")}
        config_two = {"state_dir": str(self.registry / "controller-two")}
        for value in (config_one, config_two):
            Path(value["state_dir"]).mkdir(mode=0o700)
        self.settings["controller_configs"] = ["/one", "/two"]
        config_one["wx_cli"] = config_two["wx_cli"] = self.settings["wx_cli"]
        calls = []
        with mock.patch.object(conv.ctl, "config", side_effect=lambda path, **_: config_one if str(path) == "/one" else config_two), \
             mock.patch.object(conv.interactive, "select",
                               side_effect=lambda value, talker, compatible=(): calls.append(
                                   ("select", talker, compatible))), \
             mock.patch.object(conv.interactive, "_status", return_value={"binding": {}, "consumer_cursor": 0}), \
             mock.patch.object(conv.interactive, "_runner_state", return_value=(None, {"pending": None}, 0)), \
             mock.patch.object(conv.interactive, "_check_outbox_clear"), \
             mock.patch.object(conv.interactive, "takeover",
                               side_effect=lambda value: calls.append(
                                   ("take", value["state_dir"], value.get("_compatible_fingerprints")))), \
             mock.patch.object(conv.interactive, "handback", side_effect=lambda value: calls.append(("back", value["state_dir"]))):
            with conv.coordinated(self.settings, {"state_dir": config_one["state_dir"],
                                                  "controller_config": "/one", "talker": "one@chatroom"}):
                pass
        self.assertEqual([item[0] for item in calls], ["select", "take", "back"])
        self.assertEqual(calls[0][2], (config_one,))
        self.assertEqual(calls[1][2], (conv.interactive._fingerprint(config_one),))
        lease = Path(config_two["state_dir"]) / conv.ctl.INTERACTIVE_NAME
        write_private(lease, {"version": 1})
        with mock.patch.object(conv.ctl, "config", side_effect=lambda path, **_: config_one if str(path) == "/one" else config_two), \
             mock.patch.object(conv.interactive, "select"), \
             mock.patch.object(conv.interactive, "_status", return_value={"binding": {}, "consumer_cursor": 0}), \
             mock.patch.object(conv.interactive, "_runner_state", return_value=(None, {"pending": None}, 0)), \
             mock.patch.object(conv.interactive, "_check_outbox_clear"), \
             mock.patch.object(conv.interactive, "takeover"), \
             mock.patch.object(conv.interactive, "handback"):
            with conv.coordinated(self.settings, {"state_dir": config_one["state_dir"],
                                                  "controller_config": "/one", "talker": "one@chatroom"}):
                pass
            with self.assertRaisesRegex(conv.ConversationError, "another operator"):
                with conv.coordinated(self.settings, {"state_dir": config_two["state_dir"],
                                                      "controller_config": "/two", "talker": "two@chatroom"}):
                    pass

    def test_handback_failure_is_reported_and_not_retried(self):
        value = {"state_dir": str(self.registry / "controller")}
        Path(value["state_dir"]).mkdir(mode=0o700)
        self.settings["controller_configs"] = ["/controller"]
        value["wx_cli"] = self.settings["wx_cli"]
        with mock.patch.object(conv.ctl, "config", return_value=value), \
             mock.patch.object(conv.interactive, "select"), \
             mock.patch.object(conv.interactive, "_status", return_value={"binding": {}, "consumer_cursor": 0}), \
             mock.patch.object(conv.interactive, "_runner_state", return_value=(None, {"pending": None}, 0)), \
             mock.patch.object(conv.interactive, "_check_outbox_clear"), \
             mock.patch.object(conv.interactive, "takeover"), \
             mock.patch.object(conv.interactive, "handback", side_effect=conv.interactive.ChatError("pending", "pending send")) as back:
            with self.assertRaisesRegex(conv.ConversationError, "lease retained"):
                with conv.coordinated(self.settings, {"state_dir": value["state_dir"],
                                                      "controller_config": "/controller", "talker": "one@chatroom"}):
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
