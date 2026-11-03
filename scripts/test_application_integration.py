#!/usr/bin/env python3
"""Two local applications enqueue through the real conversations CLI and Rust journals."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.test_local_channel import GROUP, fixture
from scripts.test_multi_conversation import PRIVATE, ROOT, BINARY, private_fixture
from wx_agent.application_client import ApplicationClient, ApplicationError


def private_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


class ApplicationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise unittest.SkipTest("compiled target/debug/wx-cli is required")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wx-application-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ui = self.root / "ui"
        self.ui.mkdir(mode=0o700)
        self.wrapper = self.root / "synthetic-wx-cli"
        self.wrapper.write_text("#!" + sys.executable + "\n" +
            "import hashlib, json, pathlib, subprocess, sys\n" +
            "real=" + repr(str(BINARY)) + "\n" +
            "args=sys.argv[1:]\n" +
            "if args[:2]==['conversations','find']:\n" +
            " source=pathlib.Path(args[args.index('--source')+1])\n" +
            " app=source.parent.name\n" +
            " talker,name,kind=(('synthetic_group@chatroom','Synthetic Group','group') if app=='app_a' else ('synthetic_friend','Synthetic Friend','private'))\n" +
            " cid=hashlib.sha256(('synthetic\\0'+talker).encode()).hexdigest()[:32]\n" +
            " found=('--id' in args and args[args.index('--id')+1]==cid) or ('--name' in args and args[args.index('--name')+1].casefold() in name.casefold())\n" +
            " print(json.dumps({'match_kind':('id' if '--id' in args else 'exact') if found else 'none','matches':[{'conversation_id':cid,'talker':talker,'name':name,'kind':kind}] if found else []}))\n" +
            " sys.exit(0)\n" +
            "done=subprocess.run([real,*args],input=sys.stdin.buffer.read(),capture_output=True)\n" +
            "if len(args)>1 and args[0]=='channel' and 'status' in args and done.returncode==0:\n" +
            " value=json.loads(done.stdout); value['binding']['encrypted_snapshot']=True\n" +
            " print(json.dumps(value)); sys.stderr.buffer.write(done.stderr)\n" +
            "else:\n sys.stdout.buffer.write(done.stdout); sys.stderr.buffer.write(done.stderr)\n" +
            "sys.exit(done.returncode)\n")
        self.wrapper.chmod(0o700)
        self.agent = self.root / "synthetic-wx-agent"
        self.agent.write_text("#!" + sys.executable + "\n" +
                              "from wx_agent.cli import main\nraise SystemExit(main())\n")
        self.agent.chmod(0o700)
        self.clients = {}
        self.cids = {}
        for app, talker, kind, name in (("app_a", GROUP, "group", "Synthetic Group"),
                                        ("app_b", PRIVATE, "private", "Synthetic Friend")):
            home = self.root / app
            home.mkdir(mode=0o700)
            source = home / "synthetic"
            if kind == "group":
                fixture(source)
            else:
                private_fixture(source)
            registry = home / "registry"
            registry.mkdir(mode=0o700)
            channels = registry / "channels"
            channels.mkdir(mode=0o700)
            cid = hashlib.sha256(("synthetic\0" + talker).encode()).hexdigest()[:32]
            state = channels / cid
            bind = self.cli("channel", state, "bind", "--account", "synthetic",
                            "--talker", talker, "--session", "conversation-" + cid,
                            "--source", source)
            self.cli("channel", state, "sync")
            owner = "synthetic_" + app + "_owner"
            enrolled = self.cli("coordinator", self.ui, "bind", "--conversation-id", cid,
                                "--channel-state-dir", state, "--owner", owner,
                                "--input", "push", "--reply-policy", "authorized")
            entry = {"conversation_id": cid, "talker": talker, "name": name, "kind": kind,
                     "state_dir": str(state), "session_id": bind["session_id"],
                     "channel_id": bind["channel_id"], "imported": False,
                     "controller_config": None, "revision": 1}
            private_json(registry / "conversations.json",
                         {"version": 1, "account": "synthetic", "source": str(source),
                          "selected_id": cid, "entries": {cid: entry}})
            template = home / "sender-template.json"
            private_json(template, {"ui_dir": str(self.ui)})
            conversation_config = home / "conversations-config.json"
            private_json(conversation_config,
                         {"version": 1, "wx_cli": str(self.wrapper),
                          "registry_dir": str(registry), "account": "synthetic",
                          "source": str(source), "sender_template": str(template),
                          "sender_executable": "/usr/bin/true", "controller_configs": []})
            app_config = home / "application.json"
            private_json(app_config,
                         {"version": 1, "application_id": app, "wx_agent": str(self.agent),
                          "wx_cli": str(self.wrapper), "conversation_config": str(conversation_config),
                          "coordinator_root": str(self.ui),
                          "conversations": {cid: {"state_dir": str(state), "owner": owner,
                                                  "expected_epoch": enrolled["epoch"]}}})
            self.clients[app] = ApplicationClient(app_config)
            self.cids[app] = cid
        self.env = dict(os.environ, PYTHONPATH=str(ROOT / "packages/wx-agent/src"))

    def cli(self, section, state, *args):
        done = subprocess.run([str(BINARY), section, "--state-dir", str(state),
                               *map(str, args)], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_two_apps_concurrent_enqueue_original_ids_and_no_gui(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            pending = list(pool.map(
                lambda app: self.clients[app].send_text(self.cids[app], "notice-001", "synthetic notice"),
                ("app_a", "app_b")))
        self.assertEqual({item["request_id"] for item in pending},
                         {"app_a.notice-001", "app_b.notice-001"})
        self.assertEqual({item["status"] for item in pending}, {"pending"})
        for app in ("app_a", "app_b"):
            cid = self.cids[app]
            shown = self.clients[app].status(cid, "notice-001")
            self.assertEqual(shown["request_id"], app + ".notice-001")
            self.assertEqual((shown["status"], shown["attempt_number"]), ("managed_queued", 0))
            again = self.clients[app].send_text(cid, "notice-001", "synthetic notice")
            self.assertEqual(again["request_id"], shown["request_id"])
            with self.assertRaises(ApplicationError) as conflict:
                self.clients[app].send_text(cid, "notice-001", "changed text")
            self.assertEqual(conflict.exception.code, "text_conflict")
            with self.assertRaises(ApplicationError):
                self.clients[app].status(self.cids["app_b" if app == "app_a" else "app_a"],
                                         "notice-001")
        dispatches = self.cli("coordinator", self.ui, "dispatch", "status")["dispatches"]
        self.assertEqual({item["request_id"] for item in dispatches},
                         {"app_a.notice-001", "app_b.notice-001"})
        self.assertTrue(all(item["status"] == "queued" for item in dispatches))
        self.assertFalse((self.root / "driver-events.jsonl").exists())

    def test_example_runs_from_outside_checkout(self):
        app = "app_a"
        cid = self.cids[app]
        example = self.root / "application-notifier.py"
        shutil.copy2(ROOT / "examples/application-notifier.py", example)
        config = self.root / app / "application.json"
        done = subprocess.run([sys.executable, str(example), "--config", str(config), "send",
                               "--conversation-id", cid, "--request-id", "outside-001",
                               "--text-stdin"], input="synthetic outside checkout",
                              capture_output=True, text=True, cwd=self.root, env=self.env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["request_id"], "app_a.outside-001")

    def test_old_requests_remain_readable_after_owner_change_and_takeover(self):
        for app in ("app_a", "app_b"):
            self.clients[app].send_text(self.cids[app], "notice-001", "synthetic notice")
        self.cli("coordinator", self.ui, "configure", "--conversation-id", self.cids["app_a"],
                 "--expected-epoch", 1, "--owner", "successor_owner", "--input", "pull",
                 "--reply-policy", "observe")
        self.cli("coordinator", self.ui, "takeover", "--conversation-id", self.cids["app_b"],
                 "--expected-epoch", 1)
        for app in ("app_a", "app_b"):
            cid = self.cids[app]
            shown = self.clients[app].status(cid, "notice-001")
            self.assertEqual(shown["request_id"], app + ".notice-001")
            self.assertEqual(shown["attempt_number"], 0)
            with self.assertRaises(ApplicationError) as denied:
                self.clients[app].send_text(cid, "notice-002", "must not queue")
            self.assertEqual(denied.exception.code, "owner_epoch_drift")
            missing = self.cli("coordinator", self.ui, "dispatch", "status")["dispatches"]
            self.assertFalse(any(item["request_id"] == app + ".notice-002" for item in missing))

    def test_renewed_application_grant_keeps_old_result_and_fences_new_send(self):
        app, cid = "app_a", self.cids["app_a"]
        first = self.clients[app].send_text(cid, "notice-001", "first notice")
        self.assertEqual(first["status"], "pending")
        changed = self.cli("coordinator", self.ui, "configure", "--conversation-id", cid,
                           "--expected-epoch", 1, "--owner", "successor_owner",
                           "--input", "push", "--reply-policy", "authorized")
        config_path = self.root / app / "application.json"
        config = json.loads(config_path.read_text())
        config["conversations"][cid].update(owner="successor_owner", expected_epoch=changed["epoch"])
        private_json(config_path, config)
        renewed = ApplicationClient(config_path)
        old = renewed.status(cid, "notice-001")
        self.assertEqual((old["request_id"], old["attempt_number"]), ("app_a.notice-001", 0))
        second = renewed.send_text(cid, "notice-002", "second notice")
        self.assertEqual((second["request_id"], second["status"]), ("app_a.notice-002", "pending"))
        state = config["conversations"][cid]["state_dir"]
        for key, owner, epoch in (("notice-001", "synthetic_app_a_owner", 1),
                                  ("notice-002", "successor_owner", changed["epoch"])):
            shown = self.cli("outbox", state, "show", "--request-id", "app_a." + key)
            self.assertEqual((shown["publisher_id"], shown["managed_owner"], shown["managed_epoch"]),
                             ("app_a", owner, epoch))
            self.assertEqual(shown["attempt_number"], 0)


if __name__ == "__main__":
    unittest.main()
