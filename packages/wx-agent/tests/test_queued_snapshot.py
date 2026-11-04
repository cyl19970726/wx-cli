"""Synthetic only: no host, network, real inbox or model is invoked."""
import copy
import io
import json
from pathlib import Path
import socket
import shlex
import tempfile
import time
import unittest
from unittest.mock import patch

from wx_agent.codex_plugin import PluginError
from wx_agent.queued_snapshot import (QueuedSnapshotPlugin, QueueNative, encoded, main,
                                     publish, read_private, reader_command, snapshot_chunk,
                                     snapshot_payload)


class Inbox:
    def __init__(self):
        self.calls = []
        self.requests = []
        self.fail_finish = False
        self.fail_complete = False
        self.bad_page = False
        self.delivery = dict(delivery_id=8, conversation_id="a" * 32, owner="owner", epoch=1,
                             status="pending", turn_state=None, token=None, transport_token=None,
                             session_ref=None, thread_id=None, turn_id=None, cursor_start=1, cursor_end=101,
                             attachment_refs=[], draft_text=None, reason=None, condition=None, due_at=None)

    def transport_records(self, route):
        return [copy.deepcopy(self.delivery)] if self.delivery["turn_state"] in ("reserved", "recorded") else []

    def call(self, *parts):
        self.calls.append(parts)
        if parts[0] == "status":
            return dict(conversation_id="a" * 32, owner="owner", epoch=1, control="active", input="push", owner_session_ref=None)
        action = parts[1]
        if action == "show":
            return copy.deepcopy(self.delivery)
        if action == "claim":
            if self.delivery["status"] != "pending":
                return {"status": "empty"}
            self.delivery.update(status="claimed", token="SECRET-LEASE")
            return copy.deepcopy(self.delivery)
        if action == "reserve-turn":
            self.delivery.update(turn_state="reserved", transport_token="SECRET-TRANSPORT", session_ref=parts[-1])
            return dict(delivery_id=8, turn_state="reserved", already_reserved=False)
        if action == "record-turn":
            self.delivery.update(turn_state="recorded", thread_id=parts[-3], turn_id=parts[-1])
            return copy.deepcopy(self.delivery)
        if action == "finish-turn":
            if self.fail_finish:
                raise RuntimeError("lost finish")
            self.delivery["turn_state"] = parts[-1]
            return copy.deepcopy(self.delivery)
        raise AssertionError(parts)

    def request(self, method, params):
        self.requests.append((method, copy.deepcopy(params)))
        if method == "events":
            after = params["after"]
            end = min(after + params["limit"], 101)
            events = [{"cursor": n, "text": "合成🙂" * (2500 if n == 1 else 1)} for n in range(after + 1, end + 1)]
            if self.bad_page:
                events[0]["cursor"] += 1
            return dict(conversation_id="a" * 32, delivery_id=8, events=events, next_cursor=end, has_more=end < 101)
        if method == "complete":
            if self.fail_complete:
                raise RuntimeError("unknown complete")
            self.delivery.update(status={"skip": "skipped", "defer": "deferred", "draft": "draft"}[params["decision"]],
                                 reason=params["reason"], token=None, draft_text=params.get("text"),
                                 condition=params.get("condition"), due_at=params.get("due_at"))
            return dict(delivery_id=8, status=self.delivery["status"], decision=params["decision"])
        raise AssertionError(method)


class Native:
    def __init__(self, root):
        self.thread = {"id": "root-session", "cwd": str(root), "turns": []}
        self.calls = []
        self.queue = None
        self.bad_receipt = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/read":
            return {"thread": copy.deepcopy(self.thread)}
        if method == "thread/queue/add":
            self.queue = params
            return {"queuedSubmission": {"clientUserMessageId": "wrong" if self.bad_receipt else params["clientUserMessageId"]}}
        raise AssertionError(method)


class QueuedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / "private"
        self.runtime.mkdir(mode=0o700)
        self.cli = self.root / "wx-agent-queue-session"
        self.cli.write_text("synthetic only")
        self.cli.chmod(0o700)
        self.base = self.root / "base.json"
        self.base.write_text(json.dumps(dict(version=1, conversation_config=str(self.root / "channel.json"),
            wx_agent="/bin/echo", codex="/bin/echo", bindings=[dict(session_id="root-session", conversation_id="a" * 32,
            owner="owner", expected_epoch=1, project=str(self.root), agent_host=socket.gethostname())])))
        self.base.chmod(0o600)
        self.config = self.root / "queued.json"
        self.config.write_text(json.dumps(dict(version=1, base_config=str(self.base), runtime_dir=str(self.runtime), snapshot_cli=str(self.cli))))
        self.inbox = Inbox()
        self.native = Native(self.root)
        self.plugin = self.new_plugin()

    def new_plugin(self):
        return QueuedSnapshotPlugin(self.config, inbox=self.inbox, native_factory=lambda _: self.native)

    def prepare(self, decision="skip"):
        result = self.plugin.notify_once()
        self.assertEqual(result["notifications"][0]["status"], "queued_for_host", result)
        self.note = read_private(next(self.runtime.glob("*.transport.json")))
        note = self.note
        output = dict(protocol_version=1, ref=note["ref"], snapshot_sha256=note["snapshot_sha256"], decision=decision, reason="synthetic observation")
        if decision == "draft":
            output["draft"] = "draft only"
        if decision == "defer":
            output.update(condition="manual review", due_at=1)
        items = [{"type": "userMessage", "clientId": note["client_id"], "content": self.native.queue["input"]}]
        offset = 0
        while True:
            chunk = snapshot_chunk(note["snapshot_file"], note["ref"], offset)
            items.append(dict(type="commandExecution", command=reader_command(self.cli, note, offset),
                              cwd=str(self.root), status="completed", exitCode=0, aggregatedOutput=json.dumps(chunk)))
            offset = chunk["next_offset"]
            if not chunk["has_more"]:
                break
        items.append(dict(type="agentMessage", phase="final_answer", text=json.dumps(output)))
        self.turn = dict(id="actual-turn", status="completed", startedAt=int(time.time()), completedAt=int(time.time()), items=items)
        self.native.thread["turns"] = [self.turn]
        return output

    def completes(self):
        return [x for x in self.inbox.requests if x[0] == "complete"]

    def assert_rejected(self):
        result = self.plugin.reconcile()
        self.assertEqual(result["notifications"][0]["status"], "requires_inspection", result)
        self.assertFalse(self.completes())
        self.assertEqual(self.inbox.delivery["status"], "claimed")

    def test_happy_paginated_chunked_skip_and_no_secret_artifacts(self):
        self.prepare()
        self.assertEqual(len([x for x in self.inbox.requests if x[0] == "events"]), 2)
        self.assertGreater(len(self.turn["items"]), 4)
        result = self.plugin.reconcile()
        self.assertEqual(result["notifications"][0]["reconciliations"][0]["status"], "completed")
        self.assertEqual(len(self.completes()), 1)
        self.assertEqual(self.inbox.delivery["turn_state"], "completed")
        for path in self.runtime.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("SECRET", path.read_text())
        self.assertNotIn("合成", self.native.queue["input"][0]["text"])
        self.assertTrue(all(x[0] in ("thread/read", "thread/queue/add") for x in self.native.calls))

    def test_defer_and_draft(self):
        for decision in ("defer", "draft"):
            with self.subTest(decision=decision):
                case = QueuedSnapshotTests()
                case.setUp()
                try:
                    case.prepare(decision)
                    case.plugin.reconcile()
                    self.assertEqual(case.inbox.delivery["status"], "deferred" if decision == "defer" else "draft")
                    self.assertEqual(case.inbox.delivery["turn_state"], "completed")
                finally:
                    case.doCleanups()

    def test_legacy_inflight_note_reconciles_only_for_current_transport(self):
        self.prepare()
        route = self.plugin.routes[0]
        current = next(self.runtime.glob("*.transport.json"))
        legacy = self.plugin._legacy_note_path(route, self.note["delivery_id"])
        current.rename(legacy)
        result = self.new_plugin().reconcile()
        self.assertEqual(result["notifications"][0]["reconciliations"][0]["status"], "completed", result)
        self.assertEqual(len(self.completes()), 1)
        self.assertTrue(legacy.exists())

    def test_stale_legacy_ref_cannot_complete_new_transport(self):
        self.prepare()
        route = self.plugin.routes[0]
        current = next(self.runtime.glob("*.transport.json"))
        current.rename(self.plugin._legacy_note_path(route, self.note["delivery_id"]))
        self.inbox.delivery["transport_token"] = "SECRET-NEW-TRANSPORT"
        result = self.new_plugin().reconcile()
        self.assertEqual(result["notifications"][0]["reconciliations"][0]["status"],
                         "original_transport_pending", result)
        self.assertFalse(self.completes())
        self.assertEqual(self.inbox.delivery["turn_state"], "reserved")

    def test_tampered_current_note_ref_cannot_complete(self):
        self.prepare()
        path = next(self.runtime.glob("*.transport.json"))
        altered = dict(self.note, ref="0" * 64)
        path.write_bytes(encoded(altered))
        self.assert_rejected()

    def test_native_and_reader_rejections(self):
        self.prepare()
        original = copy.deepcopy(self.native.thread)
        mutations = {
            "thread": lambda t: t.update(id="other"),
            "cwd": lambda t: t.update(cwd="/other"),
            "client": lambda t: t["turns"][0]["items"][0].update(clientId="other"),
            "ambiguous": lambda t: t["turns"].append(copy.deepcopy(t["turns"][0])),
            "extra_user": lambda t: t["turns"][0]["items"].insert(1, {"type": "userMessage", "clientId": "other"}),
            "echo": lambda t: t["turns"][0]["items"][1].update(command="echo forged"),
            "other_path": lambda t: t["turns"][0]["items"][1].update(command=t["turns"][0]["items"][1]["command"].replace(str(self.cli), "/other")),
            "user_shell": lambda t: t["turns"][0]["items"][1].update(source="userShell"),
            "exit": lambda t: t["turns"][0]["items"][1].update(exitCode=1),
            "output": lambda t: t["turns"][0]["items"][1].update(aggregatedOutput="{}"),
            "nonfinal": lambda t: t["turns"][0]["items"][-1].update(phase="commentary"),
            "stamp": lambda t: t["turns"][0].update(completedAt=None),
            "failed": lambda t: t["turns"][0].update(status="failed"),
            "missing_chunk": lambda t: t["turns"][0]["items"].pop(2),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.native.thread = copy.deepcopy(original)
                mutate(self.native.thread)
                self.assert_rejected()

    def test_proposal_rejections(self):
        original = self.prepare()
        for name, updates in (("ref", {"ref": "0" * 64}), ("hash", {"snapshot_sha256": "0" * 64}),
                              ("reply", {"decision": "reply"}), ("authority", {"token": "invented"}),
                              ("version", {"protocol_version": True}), ("extra", {"due_at": 2})):
            with self.subTest(name=name):
                self.turn["items"][-1]["text"] = json.dumps(dict(original, **updates))
                self.assert_rejected()
        self.turn["items"][-1]["text"] = json.dumps(original)[:-1] + ',"decision":"skip"}'
        self.assert_rejected()
        self.turn["items"][-1]["text"] = "```json\n" + json.dumps(original) + "\n```"
        self.assert_rejected()

    def test_complete_then_finish_failure_recovers_without_resubmit(self):
        self.prepare()
        self.inbox.fail_finish = True
        self.plugin.reconcile()
        self.assertEqual(len(self.completes()), 1)
        self.assertEqual(self.inbox.delivery["status"], "skipped")
        before_claims = len([c for c in self.inbox.calls if c[1] == "claim"])
        before_queue = len([c for c in self.native.calls if c[0] == "thread/queue/add"])
        self.inbox.fail_finish = False
        self.new_plugin().notify_once()
        self.assertEqual(len(self.completes()), 1)
        self.assertEqual(self.inbox.delivery["turn_state"], "completed")
        self.assertEqual(before_claims, len([c for c in self.inbox.calls if c[1] == "claim"]))
        self.assertEqual(before_queue, len([c for c in self.native.calls if c[0] == "thread/queue/add"]))

    def test_unknown_complete_never_retried(self):
        self.prepare()
        self.inbox.fail_complete = True
        self.plugin.reconcile()
        self.inbox.fail_complete = False
        self.new_plugin().notify_once()
        self.assertEqual(len(self.completes()), 1)
        self.assertEqual(self.inbox.delivery["status"], "claimed")

    def test_hook_reservation_not_adopted(self):
        self.inbox.delivery.update(turn_state="reserved", status="claimed", session_ref=self.plugin.routes[0].session_ref)
        self.plugin.notify_once()
        self.assertFalse(self.native.queue)
        self.assertFalse(any(c[1] == "claim" for c in self.inbox.calls))

    def test_queue_receipt_unknown_never_requeues(self):
        self.native.bad_receipt = True
        self.plugin.notify_once()
        self.new_plugin().notify_once()
        self.assertEqual(len([c for c in self.native.calls if c[0] == "thread/queue/add"]), 1)
        self.assertFalse(self.completes())

    def test_page_gap_fails_without_queue(self):
        self.inbox.bad_page = True
        self.plugin.notify_once()
        self.assertIsNone(self.native.queue)
        self.assertEqual(self.inbox.delivery["turn_state"], "reserved")

    def test_snapshot_cli_never_opens_base_config(self):
        self.prepare()
        with patch("wx_agent.queued_snapshot.CodexPlugin", side_effect=AssertionError("credentials accessed")), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["snapshot", "--file", self.note["snapshot_file"], "--ref", self.note["ref"], "--offset", "0"]), 0)
            chunk = json.loads(out.getvalue())
            self.assertTrue(chunk["has_more"])

    def test_utf8_boundaries_and_file_safety(self):
        ref = "b" * 64
        path = self.runtime / "example.json"
        publish(path, dict(protocol_version=1, ref=ref, payload={"text": "🙂中文" * 20}))
        payload = snapshot_payload(path, ref)
        result, offset = b"", 0
        while offset < len(payload):
            chunk = snapshot_chunk(path, ref, offset, 7)
            result += chunk["content_chunk"].encode()
            self.assertLessEqual(chunk["byte_end"] - offset, 7)
            offset = chunk["next_offset"]
        self.assertEqual(result, payload)
        with self.assertRaises(FileExistsError):
            publish(path, {})
        link = self.runtime / "linked.json"
        link.symlink_to(path)
        with self.assertRaises(PluginError):
            snapshot_chunk(link, ref)
        path.chmod(0o644)
        with self.assertRaises(PluginError):
            snapshot_chunk(path, ref)

    def test_snapshot_mutation_and_frozen_header_change_rejected(self):
        self.prepare()
        original = self.inbox.delivery["cursor_end"]
        self.inbox.delivery["cursor_end"] += 1
        self.assert_rejected()
        self.inbox.delivery["cursor_end"] = original
        path = Path(self.note["snapshot_file"])
        envelope = json.loads(path.read_text())
        envelope["payload"]["events"][0]["text"] = "changed"
        path.write_bytes(encoded(envelope))
        self.assert_rejected()

    def test_duplicate_final_and_reader_chunk_rejected(self):
        self.prepare()
        self.turn["items"].append(copy.deepcopy(self.turn["items"][-1]))
        self.assert_rejected()
        self.turn["items"].pop()
        self.turn["items"].insert(2, copy.deepcopy(self.turn["items"][1]))
        self.assert_rejected()

    def test_oversize_does_not_publish_or_queue(self):
        with patch("wx_agent.queued_snapshot.MAX_SNAPSHOT", 1000):
            self.plugin.notify_once()
        self.assertEqual(self.inbox.delivery["turn_state"], "reserved")
        self.assertIsNone(self.native.queue)
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.assertFalse(self.completes())

    def test_final_alias_and_default_text_elements(self):
        self.prepare()
        self.turn["items"][-1]["phase"] = "final"
        del self.turn["items"][0]["content"][0]["text_elements"]
        self.plugin.reconcile()
        self.assertEqual(self.inbox.delivery["turn_state"], "completed")

    def test_completed_business_without_our_submission_not_finished(self):
        self.prepare()
        self.inbox.delivery.update(status="skipped", reason="synthetic observation", token=None)
        result = self.plugin.reconcile()
        self.assertEqual(result["notifications"][0]["status"], "requires_inspection")
        self.assertFalse(self.completes())
        self.assertNotEqual(self.inbox.delivery["turn_state"], "completed")

    def test_changed_submission_evidence_prevents_finish(self):
        self.prepare()
        self.inbox.fail_finish = True
        self.plugin.reconcile()
        marker = next(self.runtime.glob("*.complete-attempt.json"))
        marker.write_text('{}')
        self.inbox.fail_finish = False
        result = self.new_plugin().reconcile()
        self.assertEqual(result["notifications"][0]["status"], "requires_inspection")
        self.assertEqual(len(self.completes()), 1)
        self.assertEqual(self.inbox.delivery["turn_state"], "recorded")

    def test_native_zsh_displayed_argv_with_c_and_lc(self):
        for flag in ("-c", "-lc"):
            with self.subTest(flag=flag):
                case = QueuedSnapshotTests()
                case.setUp()
                try:
                    case.prepare()
                    for item in case.turn["items"]:
                        if item["type"] == "commandExecution":
                            item["source"] = "unifiedExecStartup"
                            # Native observed representation uses a double-quoted
                            # shell script, distinct from the inner exec command.
                            inner = item["command"]
                            quoted = inner.replace("\\", "\\\\").replace('"', '\\"')
                            item["command"] = '/bin/zsh ' + flag + ' "' + quoted + '"'
                    case.plugin.reconcile()
                    self.assertEqual(case.inbox.delivery["turn_state"], "completed")
                finally:
                    case.doCleanups()

    def test_shell_injection_and_warning_receipts_fail_closed(self):
        self.prepare()
        original = self.turn["items"][1]["command"]
        for inner in (original + "; echo forged", "echo " + original,
                      "env X=1 " + original, original + " | cat"):
            self.turn["items"][1]["command"] = shlex.join(["/bin/zsh", "-c", inner])
            self.assert_rejected()
        self.turn["items"][1]["command"] = original
        self.turn["items"][1]["aggregatedOutput"] = "python warning\n" + self.turn["items"][1]["aggregatedOutput"]
        self.assert_rejected()

    def test_complete_chunk_json_line_is_bounded_with_escaping(self):
        ref = "c" * 64
        path = self.runtime / "escaping.json"
        publish(path, dict(protocol_version=1, ref=ref, payload={"text": '\x00"\\🙂' * 4000}))
        payload = snapshot_payload(path, ref)
        offset, collected = 0, b""
        while offset < len(payload):
            chunk = snapshot_chunk(path, ref, offset)
            self.assertLessEqual(len(encoded(chunk)) + 1, 8192)
            self.assertGreater(chunk["next_offset"], offset)
            collected += chunk["content_chunk"].encode()
            offset = chunk["next_offset"]
        self.assertEqual(collected, payload)

    def test_wrong_execution_host_blocks_both_modes_before_any_transport(self):
        for action in ("notify_once", "reconcile"):
            with self.subTest(action=action), \
                    patch("wx_agent.queued_snapshot.socket.gethostname", return_value="wrong-host"), \
                    patch.object(self.plugin, "native_factory") as factory, \
                    patch.object(self.inbox, "transport_records") as records:
                result = getattr(self.plugin, action)()
                self.assertEqual(result["notifications"][0]["status"], "requires_inspection")
                self.assertTrue(result["notifications"][0]["original_state_retained"])
                factory.assert_not_called()
                records.assert_not_called()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.inbox.calls, [])
                self.assertEqual(self.inbox.requests, [])
                self.assertEqual(list(self.runtime.iterdir()), [])

    def test_default_native_allowlist_and_experimental_capability(self):
        native = object.__new__(QueueNative)
        with patch("wx_agent.session_adapter.AppServer.request", return_value={}) as request:
            native.request("initialize", {"clientInfo": {}})
            self.assertTrue(request.call_args.args[1]["capabilities"]["experimentalApi"])
            for method in ("thread/start", "thread/resume", "turn/start", "thread/archive"):
                with self.assertRaises(PluginError):
                    native.request(method, {})


if __name__ == "__main__":
    unittest.main()
