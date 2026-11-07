"""Bounded foreground notification loop for an existing Codex Session host."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time

from . import wechat_agent_ctl as ctl


KNOWN_STATUSES = {"empty", "queued_for_host", "original_transport_pending",
                  "outgoing_reply_pending", "notification_unknown", "requires_inspection"}
ATTENTION_STATUSES = {"notification_unknown", "requires_inspection"}


class PluginRunnerError(RuntimeError):
    pass


def _runtime_dir(config_path: Path) -> Path:
    if not config_path.is_absolute() or config_path.resolve() != config_path:
        raise PluginRunnerError("invalid_config_path")
    ctl.private(config_path)
    digest = hashlib.sha256(str(config_path).encode()).hexdigest()[:20]
    path = config_path.parent / (".wx-agent-plugin-runner-" + digest)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    ctl.private(path, True)
    return path


@contextmanager
def _exclusive(runtime: Path):
    path = runtime / "runner.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        ctl.private(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PluginRunnerError("runner_busy") from exc
        yield
    finally:
        os.close(fd)


def _summary(value):
    if not isinstance(value, dict) or not isinstance(value.get("notifications"), list):
        raise PluginRunnerError("invalid_notification_result")
    counts = {}
    for item in value["notifications"]:
        if not isinstance(item, dict):
            raise PluginRunnerError("invalid_notification_result")
        status = item.get("status")
        status = status if status in KNOWN_STATUSES else "requires_inspection"
        counts[status] = counts.get(status, 0) + 1
    return {"total": len(value["notifications"]), "statuses": counts}


def run(config_path, *, seconds=3600, interval=2, plugin_factory=None,
        monotonic=time.monotonic, wall_time=time.time, sleep=None,
        install_signals=True):
    if (type(seconds) is not int or not 1 <= seconds <= 86400
            or type(interval) not in (int, float) or not math.isfinite(interval) or interval < 1):
        raise PluginRunnerError("invalid_duration")
    if plugin_factory is None:
        from .queued_snapshot import QueuedSnapshotPlugin
        plugin_factory = QueuedSnapshotPlugin
    runtime = _runtime_dir(Path(config_path))
    with _exclusive(runtime):
        stop_requested = threading.Event()
        wait = stop_requested.wait if sleep is None else sleep

        def stop_after_cycle(*_):
            stop_requested.set()

        previous = {}
        try:
            if install_signals:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.getsignal(signum)
                    signal.signal(signum, stop_after_cycle)
            started = wall_time()
            deadline = monotonic() + seconds
            health = {"version": 1, "run_state": "running", "pid": os.getpid(),
                      "started_at": started, "stopped_at": None, "updated_at": started,
                      "cycles": 0, "error_count": 0, "last_notifications": None,
                      "attention": False, "last_error_type": None}

            def write_health():
                health["updated_at"] = wall_time()
                ctl.atomic_json(runtime / "health.json", health)

            write_health()
            failed = False
            try:
                try:
                    plugin = plugin_factory(Path(config_path))
                except Exception as exc:
                    failed = True
                    health["error_count"] += 1
                    health["last_error_type"] = type(exc).__name__
                    health["attention"] = True
                    write_health()
                    plugin = None
                while plugin is not None and not stop_requested.is_set() and monotonic() < deadline:
                    try:
                        summary = _summary(plugin.notify_once())
                        health["last_notifications"] = summary
                        health["last_error_type"] = None
                        health["attention"] = any(key in ATTENTION_STATUSES for key in summary["statuses"])
                    except Exception as exc:
                        failed = True
                        health["error_count"] += 1
                        health["last_error_type"] = type(exc).__name__
                        health["attention"] = True
                    health["cycles"] += 1
                    write_health()
                    remaining = deadline - monotonic()
                    if stop_requested.is_set() or remaining <= 0:
                        break
                    wait(min(interval, remaining))
            except BaseException as exc:
                failed = True
                health["last_error_type"] = type(exc).__name__
                health["attention"] = True
                raise
            finally:
                health["run_state"] = "stopping"
                write_health()
                health["run_state"] = "stopped"
                health["stopped_at"] = wall_time()
                write_health()
            return 1 if failed else 0
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--adapter", choices=("queue-snapshot",), default="queue-snapshot")
    args = parser.parse_args(argv)
    try:
        from .queued_snapshot import QueuedSnapshotPlugin
        factory = QueuedSnapshotPlugin
        return run(args.config, seconds=args.seconds, interval=args.interval, plugin_factory=factory)
    except Exception as exc:
        print("plugin runner: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
