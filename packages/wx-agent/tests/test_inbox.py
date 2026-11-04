"""Push routing boundaries without a model, desktop or WeChat process."""
import socket
import json
import fcntl
import os
from contextlib import ExitStack, nullcontext
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock
import sys

from wx_agent import inbox
from wx_agent import conversations
from wx_agent import session_adapter


class CoreBoundaryTests(unittest.TestCase):
    def test_legacy_exports_are_the_optional_implementations(self):
        from wx_agent.integrations import codex
        from wx_agent import errors
        self.assertIs(inbox.PushLoop, codex.PushLoop)
        self.assertIs(inbox.provision, codex.provision)
        self.assertIs(session_adapter.SessionError, errors.SessionError)

    def test_legacy_provision_cli_routes_to_optional_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sessions = root / "sessions.json"
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(conversations, "config", return_value={"wx_cli": "/bin/false"}))
                stack.enter_context(mock.patch.object(inbox.dispatch, "root_from_settings", return_value=root))
                provision = stack.enter_context(mock.patch.object(inbox, "provision", return_value={"provisioned": True}))
                result = inbox.main(["--config", str(root / "config.json"), "provision",
                                     "--session-config", str(sessions), "--owner", "owner1",
                                     "--cwd", str(root), "--codex", "synthetic-codex"])
            self.assertEqual(result, 0)
            provision.assert_called_once_with(sessions, "owner1", root, "synthetic-codex", [])

    def test_claim_cli_fences_optional_epoch_without_changing_legacy_call(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary).resolve()
            stack.enter_context(mock.patch.object(conversations, "config", return_value={"wx_cli": "/bin/false"}))
            stack.enter_context(mock.patch.object(inbox.dispatch, "root_from_settings", return_value=root))
            call = stack.enter_context(mock.patch.object(inbox.dispatch, "call", return_value={"status": "pending"}))
            base = ["--config", str(root / "config.json"), "claim", "--id", "group1", "--owner", "owner1"]
            self.assertEqual(inbox.main(base + ["--expected-epoch", "1"]), 0)
            self.assertEqual(call.call_args.args[2:],
                             ("inbox", "claim", "--conversation-id", "group1", "--owner", "owner1",
                              "--expected-epoch", "1"))
            self.assertEqual(inbox.main(base), 0)
            self.assertEqual(call.call_args.args[2:],
                             ("inbox", "claim", "--conversation-id", "group1", "--owner", "owner1"))

    def test_fresh_collector_process_never_imports_provider_or_starts_codex(self):
        script = textwrap.dedent('''
            import importlib.abc, json, sys, tempfile
            from contextlib import ExitStack
            from pathlib import Path
            from unittest import mock

            blocked = {'wx_agent.session_adapter', 'wx_agent.integrations.codex',
                       'wx_agent.codex_reply_agent'}
            class DenyProvider(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname in blocked:
                        raise AssertionError('provider imported: ' + fullname)
            sys.meta_path.insert(0, DenyProvider())

            from wx_agent import inbox, conversations
            assert not blocked.intersection(sys.modules)

            class Executor:
                exitcode = None
                def poll(self): return self.exitcode
                def terminate(self): pass
                def wait(self, timeout):
                    self.exitcode = 0
                    return 0
            def start(argv, **kwargs):
                assert argv[2:4] == ['wx_agent.cli', 'dispatch'], argv
                return Executor()

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                config = root / 'config.json'
                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(conversations, 'config', return_value={'wx_cli': '/bin/false'}))
                    stack.enter_context(mock.patch.object(inbox.dispatch, 'root_from_settings', return_value=root))
                    stack.enter_context(mock.patch.object(inbox.dispatch, 'call', return_value={'paused_reason': 'test', 'dispatches': []}))
                    stack.enter_context(mock.patch.object(inbox, 'collect', return_value=[]))
                    stack.enter_context(mock.patch.object(inbox.subprocess, 'Popen', side_effect=start))
                    assert inbox.main(['--config', str(config), 'status', '--id', 'synthetic']) == 0
                    assert inbox.main(['--config', str(config), 'run', '--once', '--seconds', '5']) == 0
                health = json.loads((root / 'inbox-health.json').read_text())
                assert health['run_state'] == 'stopped'
                assert health['collector_alive'] is False
                assert health['executor_alive'] is False
                assert not blocked.intersection(sys.modules), blocked.intersection(sys.modules)
        ''')
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class FakeServer:
    def __init__(self, *_):
        pass

    def close(self):
        pass


class FakeAdapter:
    state = "idle"
    failure = None
    deliveries = []
    turn_status_value = "completed"

    def __init__(self, session, *_):
        self.session = session

    def status(self):
        return {"state": self.state}

    def deliver(self, delivery):
        self.deliveries.append(delivery.delivery_id)
        if self.failure:
            raise self.failure
        return {"turn_id": "test-turn"}

    def close(self):
        pass

    def turn_status(self, turn_id):
        return {"turn_id": turn_id, "status": self.turn_status_value}


class PushLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        FakeAdapter.state = "idle"
        FakeAdapter.failure = None
        FakeAdapter.deliveries = []
        FakeAdapter.turn_status_value = "completed"
        self.calls = []
        self.claims = 0
        self.previous = []
        self.patches = [
            mock.patch.object(session_adapter, "AppServer", FakeServer),
            mock.patch.object(session_adapter, "SessionAdapter", FakeAdapter),
            mock.patch.object(session_adapter, "RustDeliveryEvidence", lambda *_: object()),
            mock.patch.object(inbox.dispatch, "call", side_effect=self.coordinator),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.loop = inbox.PushLoop("/bin/false", Path(self.temp.name),
                                   Path(self.temp.name) / "config.json",
                                   {"owner1": dict(provider="codex", host=socket.gethostname(),
                                                   thread_id="thread1", cwd=self.temp.name,
                                                   lock_path=str(Path(self.temp.name) / "lock"))})
        self.addCleanup(self.loop.close)

    def coordinator(self, _binary, _root, *args):
        self.calls.append(args)
        if args == ("status",):
            return {"conversations": ["conversation1"]}
        if args[:2] == ("status", "--conversation-id"):
            return {"owner": "owner1", "input": "push", "control": "active", "epoch": 1}
        if args[:2] == ("inbox", "status"):
            return {"deliveries": self.previous}
        if args[:2] == ("inbox", "show"):
            return {"delivery_id": 42, "turn_id": "turn-42", "thread_id": "thread1",
                    "transport_token": "synthetic-transport-token", "status": "claimed"}
        if args[:2] == ("inbox", "finish-turn"):
            return {"delivery_id": 42, "turn_state": "completed"}
        if args[:2] == ("inbox", "claim"):
            self.assertEqual(args[args.index("--expected-epoch") + 1], 1)
            self.claims += 1
            return {"status": "claimed", "delivery_id": 42,
                    "token": "synthetic-token", "epoch": 1,
                    "cursor_start": 4, "cursor_end": 4}
        raise AssertionError(args)

    def test_busy_session_leaves_inbox_unclaimed(self):
        FakeAdapter.state = "busy"
        self.loop.tick()
        self.assertEqual(self.claims, 0)
        self.assertEqual(FakeAdapter.deliveries, [])

    def test_unknown_delivery_requires_review_before_another_claim(self):
        FakeAdapter.failure = session_adapter.DeliveryUnknown("synthetic disconnect")
        self.assertEqual(self.loop.tick()["attention"],
                         {"owner1": "delivery requires inspection; no replacement turn started"})
        self.assertEqual(self.claims, 1)
        self.loop.tick()
        self.assertEqual(self.claims, 1, "unknown delivery must stop later claims for this owner")
        self.assertEqual(FakeAdapter.deliveries, ["42"])

    def test_restart_reconciles_original_recorded_turn_without_new_delivery(self):
        session = self.loop.adapters["owner1"].session
        self.previous = [{"delivery_id": 42, "turn_state": "recorded",
                          "session_ref": session.session_ref,
                          "thread_id": session.thread_id, "turn_id": "turn-42"}]
        result = self.loop.tick(allow_new=False)
        self.assertEqual(result["active_sessions"], 0)
        self.assertEqual(FakeAdapter.deliveries, [])
        self.assertEqual(self.claims, 0)
        self.assertTrue(any(args[:2] == ("inbox", "finish-turn") for args in self.calls))

    def test_restart_keeps_unmapped_reservation_for_inspection(self):
        session = self.loop.adapters["owner1"].session
        self.previous = [{"delivery_id": 42, "turn_state": "reserved",
                          "session_ref": session.session_ref,
                          "thread_id": None, "turn_id": None}]
        result = self.loop.tick(allow_new=False)
        self.assertIn("owner1", result["attention"])
        self.assertEqual(FakeAdapter.deliveries, [])
        self.assertEqual(self.claims, 0)

    def test_restart_does_not_touch_recorded_turn_while_other_transport_holds_lock(self):
        session = self.loop.adapters["owner1"].session
        self.previous = [{"delivery_id": 42, "turn_state": "recorded",
                          "session_ref": session.session_ref,
                          "thread_id": session.thread_id, "turn_id": "turn-42"}]
        FakeAdapter.state = "busy"
        result = self.loop.tick(allow_new=False)
        self.assertEqual(result["active_sessions"], 0)
        self.assertFalse(any(args[:2] in (("inbox", "show"), ("inbox", "finish-turn"))
                             for args in self.calls))
        self.assertEqual(self.claims, 0)


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.path=self.root/"sessions.json"
        outer=self
        class Server:
            starts=0
            turns=0
            durable=False
            disconnect=False
            terminal="completed"
            def __init__(self,*_):
                self.loaded=False
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def request(self,method,params):
                if method=="thread/start":
                    Server.starts+=1;self.loaded=True
                    return {"thread":{"id":"thread-init"}}
                if method=="turn/start":
                    saved=json.loads(outer.path.read_text())
                    outer.assertEqual(saved["sessions"]["owner1"]["thread_id"],"thread-init")
                    outer.assertEqual(saved["provisioning"]["owner1"]["state"],"starting")
                    outer.assertEqual(params["sandboxPolicy"]["type"],"readOnly")
                    Server.turns+=1
                    if Server.disconnect:raise session_adapter.DeliveryUnknown("synthetic response lost")
                    Server.durable=True
                    return {"turn":{"id":"init-turn"}}
                if method=="thread/resume":
                    if not Server.durable:raise session_adapter.SessionError("no rollout")
                    self.loaded=True
                    return {"thread":{"id":"thread-init","status":{"type":"idle"}}}
                if method=="thread/read":
                    if not self.loaded:raise session_adapter.SessionError("thread not loaded")
                    return {"thread":{"id":"thread-init","status":{"type":"idle"},
                                      "turns":[{"id":"init-turn","status":Server.terminal}]}}
                raise AssertionError(method)
        self.server=Server
        patch=mock.patch.object(session_adapter,"AppServer",Server)
        patch.start();self.addCleanup(patch.stop)

    def test_explicit_provision_persists_original_reference_and_verifies_cold_resume(self):
        result=inbox.provision(self.path,"owner1",self.root,"codex")
        self.assertTrue(result["cold_resume_verified"])
        saved=json.loads(self.path.read_text())
        self.assertEqual(saved["provisioning"]["owner1"]["state"],"ready")
        self.assertEqual((self.server.starts,self.server.turns),(1,1))
        with self.assertRaises(inbox.dispatch.DispatchError):
            inbox.provision(self.path,"owner1",self.root,"codex")
        self.assertEqual(self.server.starts,1)

    def test_unknown_initialization_does_not_replace_or_replay(self):
        self.server.disconnect=True
        with self.assertRaises(session_adapter.DeliveryUnknown):
            inbox.provision(self.path,"owner1",self.root,"codex")
        saved=json.loads(self.path.read_text())
        self.assertEqual(saved["sessions"]["owner1"]["thread_id"],"thread-init")
        self.assertNotEqual(saved["provisioning"]["owner1"]["state"],"ready")
        with self.assertRaises(inbox.dispatch.DispatchError):
            inbox.provision(self.path,"owner1",self.root,"codex")
        self.assertEqual((self.server.starts,self.server.turns),(1,1))

    def test_failed_initialization_retains_exact_turn_and_cannot_claim_ready(self):
        self.server.terminal="interrupted"
        with self.assertRaises(session_adapter.SessionError):
            inbox.provision(self.path,"owner1",self.root,"codex")
        saved=json.loads(self.path.read_text())
        self.assertEqual(saved["provisioning"]["owner1"]["turn_id"],"init-turn")
        self.assertFalse(saved["provisioning"]["owner1"].get("cold_resume_verified",False))


class RunHealthTests(unittest.TestCase):
    class Executor:
        def __init__(self, timeout=False):
            self.exitcode = None
            self.timeout = timeout
            self.terminated = False

        def poll(self):
            return self.exitcode

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            if self.timeout:
                raise subprocess.TimeoutExpired("synthetic executor", timeout)
            self.exitcode = 0
            return self.exitcode

    class Future:
        def done(self):
            return True

        def result(self):
            return [{"conversation_id": "synthetic", "created": 0}]

    class Collector:
        def __init__(self, fail_shutdown=False):
            self.fail_shutdown = fail_shutdown
            self._threads = []

        def submit(self, *_):
            return RunHealthTests.Future()

        def shutdown(self, wait):
            if self.fail_shutdown:
                raise OSError("synthetic collector cleanup error")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "config.json"
        self.executor = self.Executor()
        self.collector = self.Collector()
        self.health_states = []

    def run_once(self, status_error=None, lock_error=None, push=None, lock_context=None):
        def coordinator(_binary, _root, *args):
            self.assertEqual(args, ("dispatch", "status"))
            if status_error:
                raise status_error
            return {"paused_reason": None, "dispatches": []}

        with ExitStack() as stack:
            atomic_json = inbox.ctl.atomic_json
            def record_health(path, value):
                if Path(path).name == "inbox-health.json":
                    self.health_states.append(value.get("run_state"))
                return atomic_json(path, value)
            stack.enter_context(mock.patch.object(conversations, "config", return_value={"wx_cli": "/bin/false"}))
            stack.enter_context(mock.patch.object(inbox.dispatch, "root_from_settings", return_value=self.root))
            if lock_error:
                stack.enter_context(mock.patch.object(inbox.dispatch, "exclusive", side_effect=lock_error))
            else:
                stack.enter_context(mock.patch.object(inbox.dispatch, "exclusive",
                                                      return_value=lock_context or nullcontext()))
            stack.enter_context(mock.patch.object(inbox.dispatch, "call", side_effect=coordinator))
            self.popen_mock = stack.enter_context(mock.patch.object(inbox.subprocess, "Popen", return_value=self.executor))
            stack.enter_context(mock.patch.object(inbox, "ThreadPoolExecutor", return_value=self.collector))
            stack.enter_context(mock.patch.object(inbox.signal, "signal"))
            stack.enter_context(mock.patch.object(inbox.ctl, "atomic_json", side_effect=record_health))
            args = ["--config", str(self.config), "run", "--once", "--seconds", "5"]
            if push is not None:
                stack.enter_context(mock.patch.object(inbox.ctl, "read_json", return_value={"version": 1, "sessions": {}}))
                stack.enter_context(mock.patch.object(inbox, "PushLoop", return_value=push))
                args += ["--session-config", str(self.root / "sessions.json")]
            return inbox.main(args)

    def health(self):
        return json.loads((self.root / "inbox-health.json").read_text())

    def test_normal_stop_overwrites_running_health_after_executor_exits(self):
        self.assertEqual(self.run_once(), 0)
        health = self.health()
        self.assertEqual(health["run_state"], "stopped")
        self.assertIsNotNone(health["stopped_at"])
        self.assertFalse(health["executor_alive"])
        self.assertEqual(health["executor_exitcode"], 0)
        self.assertFalse(health["collector_alive"])
        self.assertFalse(health["collection_active"])
        self.assertEqual(health["cleanup_errors"], [])
        self.assertTrue(self.executor.terminated)
        self.assertEqual(self.health_states, ["running", "stopping", "stopped"])

    def test_executor_timeout_keeps_stopping_health_and_reports_incomplete_shutdown(self):
        self.executor.timeout = True
        with self.assertRaisesRegex(RuntimeError, "shutdown incomplete"):
            self.run_once()
        health = self.health()
        self.assertEqual(health["run_state"], "stopping")
        self.assertIsNone(health["stopped_at"])
        self.assertTrue(health["executor_alive"])
        self.assertIsNone(health["executor_exitcode"])
        self.assertTrue(health["executor_wait_timed_out"])

    def test_cleanup_error_is_recorded_without_masking_run_error(self):
        self.collector.fail_shutdown = True
        self.assertEqual(self.run_once(status_error=ValueError("synthetic run failure")), 1)
        health = self.health()
        self.assertEqual(health["run_state"], "stopping")
        self.assertEqual(health["run_error"], "ValueError")
        self.assertEqual(health["cleanup_errors"],
                         [{"step": "collector_shutdown", "error": "OSError"}])
        self.assertFalse(health["executor_alive"])
        self.assertFalse(health["collector_alive"])

    def test_cleanup_error_after_normal_run_is_not_reported_stopped(self):
        self.collector.fail_shutdown = True
        with self.assertRaisesRegex(RuntimeError, "shutdown failed: collector_shutdown"):
            self.run_once()
        health = self.health()
        self.assertEqual(health["run_state"], "stopping")
        self.assertIsNone(health["stopped_at"])
        self.assertEqual(health["cleanup_errors"],
                         [{"step": "collector_shutdown", "error": "OSError"}])

    def test_unhandled_run_error_survives_cleanup_error(self):
        self.collector.fail_shutdown = True
        with self.assertRaisesRegex(RuntimeError, "synthetic unhandled failure"):
            self.run_once(status_error=RuntimeError("synthetic unhandled failure"))
        health = self.health()
        self.assertEqual(health["run_state"], "stopping")
        self.assertEqual(health["run_error"], "RuntimeError")
        self.assertEqual(health["cleanup_errors"],
                         [{"step": "collector_shutdown", "error": "OSError"}])

    def test_busy_runner_lock_cannot_overwrite_other_listeners_health(self):
        existing = self.root / "inbox-health.json"
        original = b'{"version":1,"run_state":"running","executor_alive":true}\n'
        existing.write_bytes(original)
        self.assertEqual(self.run_once(lock_error=inbox.dispatch.DispatchError("synthetic lock busy")), 1)
        self.assertEqual(existing.read_bytes(), original)
        self.assertEqual(self.health_states, [])
        self.popen_mock.assert_not_called()
        self.assertFalse(self.executor.terminated)

    def test_old_runner_never_overwrites_new_owner_after_lock_release(self):
        health_path = self.root / "inbox-health.json"
        replacement = b'{"version":1,"run_state":"running","pid":99999}\n'
        class Lock:
            def __enter__(self):
                return None
            def __exit__(self, *_):
                states = self_test.health_states[:]
                self_test.assertEqual(states, ["running", "stopping", "stopped"])
                health_path.write_bytes(replacement)
        self_test = self
        self.assertEqual(self.run_once(lock_context=Lock()), 0)
        self.assertEqual(health_path.read_bytes(), replacement)
        self.assertEqual(self.health_states, ["running", "stopping", "stopped"])

    def test_release_failure_records_stopping_while_lock_is_still_owned(self):
        class Lock:
            def __enter__(self):
                self.fd = os.open(self_test.root / "inbox-runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self.fd
            def __exit__(self, *_):
                raise OSError("synthetic release failure")
        self_test = self
        with self.assertRaisesRegex(RuntimeError, "runner_lock_release"):
            self.run_once(lock_context=Lock())
        health = self.health()
        self.assertEqual(health["run_state"], "stopping")
        self.assertIsNone(health["stopped_at"])
        self.assertEqual(health["cleanup_errors"],
                         [{"step": "runner_lock_release", "error": "OSError"}])

    def test_final_health_labels_push_as_last_observation_after_close(self):
        class Push:
            active = {}
            closed = False
            def tick(self, allow_new):
                return {"active_sessions": 2, "attention": {}}
            def close(self):
                self.closed = True
        push = Push()
        self.assertEqual(self.run_once(push=push), 0)
        health = self.health()
        self.assertTrue(push.closed)
        self.assertTrue(health["push_closed"])
        self.assertEqual(health["push"]["active_sessions"], 2)
        self.assertIsInstance(health["push_observed_at"], float)
        self.assertEqual(health["run_state"], "stopped")


if __name__ == "__main__":
    unittest.main()
