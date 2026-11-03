"""The notification runner owns only its loop, lock and derived health."""
from contextlib import contextmanager
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from wx_agent import plugin_runner
from wx_agent import wechat_agent_ctl as ctl


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class PluginRunnerTests(unittest.TestCase):
    def test_queue_snapshot_cli_reuses_the_bounded_runner(self):
        from wx_agent.queued_snapshot import QueuedSnapshotPlugin
        with mock.patch.object(plugin_runner, "run", return_value=0) as run:
            self.assertEqual(plugin_runner.main(["--config", "/private/scoped.json",
                "--seconds", "30", "--adapter", "queue-snapshot"]), 0)
        self.assertIs(run.call_args.kwargs["plugin_factory"], QueuedSnapshotPlugin)
        self.assertEqual(run.call_args.kwargs["seconds"], 30)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o755)
        self.config = self.root / "plugin.json"
        self.config.write_text('{"private":"do-not-copy"}')
        self.config.chmod(0o600)
        self.clock = Clock()

    def run_loop(self, factory, **kwargs):
        return plugin_runner.run(self.config, plugin_factory=factory,
                                 monotonic=lambda: self.clock.now,
                                 wall_time=lambda: 1000 + self.clock.now,
                                 sleep=self.clock.sleep, **kwargs)

    def health_file(self):
        return next(self.root.glob(".wx-agent-plugin-runner-*/health.json"))

    def test_normal_deadline_reports_sanitized_final_health_without_closing_host(self):
        host = mock.Mock()
        host.notify_once.return_value = {"notifications": [
            {"conversation_id": "a" * 32, "status": "queued_for_host", "body": "private body"},
            {"status": "empty", "token": "private token"}]}
        self.assertEqual(self.run_loop(lambda _: host, seconds=3, interval=2, install_signals=False), 0)
        health_path = self.health_file()
        health = json.loads(health_path.read_text())
        self.assertEqual(health["run_state"], "stopped")
        self.assertEqual((health["started_at"], health["stopped_at"]), (1000, 1003))
        self.assertEqual(health["cycles"], 2)
        self.assertEqual(health["last_notifications"],
                         {"total": 2, "statuses": {"queued_for_host": 1, "empty": 1}})
        self.assertEqual(self.clock.sleeps, [2, 1])
        self.assertNotIn("private", health_path.read_text())
        self.assertEqual(host.notify_once.call_count, 2)
        host.close.assert_not_called()

    def test_second_runner_busy_preserves_current_owner_health(self):
        outer = self

        class Host:
            def notify_once(self):
                before = outer.health_file().read_bytes()
                with outer.assertRaisesRegex(plugin_runner.PluginRunnerError, "runner_busy"):
                    outer.run_loop(lambda _: mock.Mock(), seconds=1, install_signals=False)
                outer.assertEqual(outer.health_file().read_bytes(), before)
                return {"notifications": []}

        self.assertEqual(self.run_loop(lambda _: Host(), seconds=1, install_signals=False), 0)
        self.assertEqual(json.loads(self.health_file().read_text())["run_state"], "stopped")

    def test_signal_finishes_current_api_cycle_without_interrupting_host(self):
        host = mock.Mock()

        def notify():
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return {"notifications": [{"status": "original_transport_pending"}]}

        host.notify_once.side_effect = notify
        previous = signal.getsignal(signal.SIGTERM)
        self.assertEqual(self.run_loop(lambda _: host, seconds=30, interval=2), 0)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        self.assertEqual(host.notify_once.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(json.loads(self.health_file().read_text())["run_state"], "stopped")
        host.close.assert_not_called()

    def test_term_wakes_real_idle_wait_and_writes_stopped_health(self):
        script = textwrap.dedent("""
            import sys
            import threading
            from pathlib import Path
            from types import SimpleNamespace
            from wx_agent import plugin_runner

            class ReadyEvent(threading.Event):
                def wait(self, timeout=None):
                    print("IDLE_WAIT", flush=True)
                    return super().wait(timeout)

            class Host:
                def notify_once(self):
                    return {"notifications": []}

            plugin_runner.threading = SimpleNamespace(Event=ReadyEvent)
            raise SystemExit(plugin_runner.run(Path(sys.argv[1]), seconds=3600,
                                               interval=3600, plugin_factory=lambda _: Host()))
        """)
        process = subprocess.Popen([sys.executable, "-u", "-c", script, str(self.config)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(ready, "runner did not enter its idle wait")
            self.assertEqual(process.stdout.readline().strip(), "IDLE_WAIT")
            time.sleep(0.05)
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertLess(time.monotonic() - started, 5)
            health = json.loads(self.health_file().read_text())
            self.assertEqual(health["run_state"], "stopped")
            self.assertEqual(health["cycles"], 1)
            self.assertIsNotNone(health["stopped_at"])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_api_error_is_static_visible_and_keeps_runner_state(self):
        host = mock.Mock()
        host.notify_once.side_effect = RuntimeError("secret response and token")
        self.assertEqual(self.run_loop(lambda _: host, seconds=1, install_signals=False), 1)
        raw = self.health_file().read_text()
        health = json.loads(raw)
        self.assertEqual(health["run_state"], "stopped")
        self.assertEqual(health["last_error_type"], "RuntimeError")
        self.assertEqual(health["error_count"], 1)
        self.assertTrue(health["attention"])
        self.assertNotIn("secret", raw)
        host.close.assert_not_called()

    def test_plugin_initialization_failure_has_final_health_and_failure_code(self):
        def unavailable(_):
            raise RuntimeError("private Codex path")

        self.assertEqual(self.run_loop(unavailable, seconds=1, install_signals=False), 1)
        raw = self.health_file().read_text()
        self.assertEqual(json.loads(raw)["run_state"], "stopped")
        self.assertEqual(json.loads(raw)["last_error_type"], "RuntimeError")
        self.assertNotIn("private Codex path", raw)
        self.assertEqual(self.clock.sleeps, [])

    def test_final_health_is_written_before_lock_release(self):
        original = plugin_runner._exclusive
        observed = []

        @contextmanager
        def new_owner_after_release(runtime):
            with original(runtime):
                yield
                observed.append(json.loads((runtime / "health.json").read_text())["run_state"])
            ctl.atomic_json(runtime / "health.json", {"run_state": "running", "pid": 999})

        host = mock.Mock()
        host.notify_once.return_value = {"notifications": []}
        with mock.patch.object(plugin_runner, "_exclusive", new_owner_after_release):
            self.assertEqual(self.run_loop(lambda _: host, seconds=1, install_signals=False), 0)
        self.assertEqual(observed, ["stopped"])
        self.assertEqual(json.loads(self.health_file().read_text()), {"run_state": "running", "pid": 999})

    def test_unsafe_runtime_and_invalid_duration_never_construct_plugin(self):
        factory = mock.Mock()
        with self.assertRaisesRegex(plugin_runner.PluginRunnerError, "invalid_duration"):
            self.run_loop(factory, seconds=0, install_signals=False)
        runtime = plugin_runner._runtime_dir(self.config)
        runtime.chmod(0o755)
        with self.assertRaises(ctl.ControlError):
            self.run_loop(factory, seconds=1, install_signals=False)
        factory.assert_not_called()
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)

    def test_runtime_symlink_is_refused_before_plugin_creation(self):
        runtime = plugin_runner._runtime_dir(self.config)
        runtime.rmdir()
        runtime.symlink_to(self.root, target_is_directory=True)
        factory = mock.Mock()
        with self.assertRaises(ctl.ControlError):
            self.run_loop(factory, seconds=1, install_signals=False)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
