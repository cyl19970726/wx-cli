#!/usr/bin/env python3
"""Offline runner regression tests. All executables and messages are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace

from wx_agent import channel_agent

RUNNER = Path(__file__).with_name("channel_agent.py")

FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ['FAKE_CHANNEL_DATA'])
d = json.loads(path.read_text())
a = sys.argv[1:]
kind = a[0]
cmd = a[3]
def value(flag): return a[a.index(flag)+1]
def save(): path.write_text(json.dumps(d))
before = d.get('fail_before',{}).get(cmd,0)
if before:
    d['fail_before'][cmd] = before - 1
    save(); sys.exit(75)
if d.get('bad_json') == cmd:
    print('{broken'); sys.exit(0)
def fail_after():
    count = d.get('fail_after',{}).get(cmd,0)
    if count:
        d['fail_after'][cmd] = count - 1
        save(); sys.exit(75)
if kind == 'channel':
    if cmd == 'status':
        print(json.dumps({'binding':d['binding'],'latest_cursor':max([x['cursor'] for x in d['events']],default=0)}))
    elif cmd == 'ack':
        c = int(value('--cursor'))
        old = d.get('acked',0)
        if c < old or c > max([x['cursor'] for x in d['events']],default=0): sys.exit(2)
        d['acked'] = c; save(); fail_after(); print(json.dumps({'cursor':c}))
    elif cmd == 'subscribe':
        for x in d['events']:
            if x['cursor'] > d.get('acked',0): print(json.dumps(x))
    elif cmd == 'events':
        after = int(value('--after')); limit = int(value('--limit'))
        for x in [e for e in d['events'] if e['cursor'] > after][:limit]: print(json.dumps(x))
    else: sys.exit(3)
elif kind == 'outbox':
    if cmd == 'list':
        print(json.dumps([{'request_id':k,'status':v['status']} for k,v in d['outbox'].items()]))
    elif cmd == 'show':
        r = value('--request-id')
        if r not in d['outbox']: sys.exit(4)
        print(json.dumps({'request_id':r,**d['outbox'][r]}))
    elif cmd == 'queue':
        r = value('--request-id'); body = sys.stdin.read()
        if r in d['outbox'] and d['outbox'][r]['text'] != body: sys.exit(5)
        d['outbox'].setdefault(r, {'text':body,'status':'queued'})
        save(); fail_after(); print(json.dumps({'request_id':r,'status':d['outbox'][r]['status']}))
    elif cmd == 'send':
        r = value('--request-id')
        if d['outbox'][r]['status'] != 'queued': sys.exit(6)
        d['sends'] += 1
        d['outbox'][r]['status'] = d.get('send_result','ui_observed')
        save()
        fail_after()
        print(json.dumps({'request_id':r,'status':d['outbox'][r]['status']}))
    else: sys.exit(7)
else: sys.exit(8)
'''

FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p = Path(os.environ['FAKE_CHANNEL_DATA'])
d = json.loads(p.read_text()); d['agent_calls'] += 1
p.write_text(json.dumps(d))
if d.get('agent_fail'): sys.exit(1)
if d.get('agent_fail_count',0):
    d['agent_fail_count'] -= 1; p.write_text(json.dumps(d)); sys.exit(1)
if d.get('agent_sleep'):
    import time
    time.sleep(d['agent_sleep'])
request = json.load(sys.stdin)
assert request['version'] == 1
d['last_request'] = request
p.write_text(json.dumps(d))
print(json.dumps({'text':'reply to '+str(request['event']['server_id'])}))
'''


def event(cursor, server_id, direction="incoming", kind="message_observed", content=None):
    return {"cursor":cursor,"server_id":server_id,"kind":kind,"direction":direction,
            "channel_id":"channel1","session_id":"session1","account":"account1",
            "talker":"test@chatroom","message":{"server_id":server_id,"talker":"test@chatroom",
            "sender":"member" if direction != "outgoing" else "self",
            "content":content or {"Text":"hello"}}}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.data_path = self.root / "fixture.json"
        self.cli = self.root / "fake-cli"
        self.agent = self.root / "fake-agent"
        for path, source in ((self.cli, FAKE_CLI), (self.agent, FAKE_AGENT)):
            path.write_text(source)
            path.chmod(0o700)
        self.data = {"binding":{"channel_id":"channel1","session_id":"session1",
                                 "account":"account1","talker":"test@chatroom",
                                 "source":"/synthetic","encrypted_snapshot":True},
                     "events":[],"acked":0,"outbox":{},"sends":0,"agent_calls":0}
        self.save()
        self.old_env = os.environ.get("FAKE_CHANNEL_DATA")
        os.environ["FAKE_CHANNEL_DATA"] = str(self.data_path)

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("FAKE_CHANNEL_DATA", None)
        else:
            os.environ["FAKE_CHANNEL_DATA"] = self.old_env
        self.temp.cleanup()

    def save(self):
        self.data_path.write_text(json.dumps(self.data))

    def write_memory(self, binding=None):
        identity = {key: self.data["binding"][key]
                    for key in ("channel_id", "session_id", "account", "talker")}
        value = {"version": 1, "binding": binding or identity, "revision": 1,
                 "summary": "Earlier: prefers Tuesday lessons", "summary_source_cursors": [1],
                 "confirmed": [{"text": "Tuesday works", "source_cursors": [1]}],
                 "open_questions": [{"text": "Which time zone?", "source_cursors": [1]}]}
        path = self.state / "agent-memory.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def test_session_memory_survives_beyond_recent_context(self):
        self.data["events"] = [event(i, i + 100) for i in range(1, 16)]
        self.save()
        self.write_memory()
        self.run_agent(start="14")
        request = self.data["last_request"]
        self.assertTrue(all(item["cursor"] > 1 for item in request["recent_context"]))
        self.assertEqual(request["session_memory"]["summary"], "Earlier: prefers Tuesday lessons")
        self.assertEqual((self.data["agent_calls"], self.data["sends"]), (1, 1))

    def test_corrupt_or_cross_bound_memory_stops_before_model_and_send(self):
        self.data["events"] = [event(1, 101)]
        self.save()
        path = self.write_memory(binding={"channel_id": "other", "session_id": "session1",
                                          "account": "account1", "talker": "test@chatroom"})
        self.run_agent(okay=False)
        self.assertEqual((self.data["agent_calls"], self.data["sends"]), (0, 0))
        path.write_text("{invalid")
        self.run_agent(start=None, okay=False)
        self.assertEqual((self.data["agent_calls"], self.data["sends"]), (0, 0))

    def test_missing_memory_keeps_existing_request_contract(self):
        self.data["events"] = [event(1, 101)]
        self.save()
        self.run_agent()
        self.assertNotIn("session_memory", self.data["last_request"])

    def run_agent(self, *extra, start="0", okay=True):
        argv = [sys.executable,str(RUNNER),"--wx-cli",str(self.cli),"--state-dir",str(self.state),
                "--consumer","test-agent","--agent-command-json",json.dumps([str(self.agent)]),
                "--sender-driver","/remote/driver","--retry-base-seconds","0.01","--once"]
        if start is not None: argv += ["--start-after",start]
        result = subprocess.run(argv + list(extra), text=True, capture_output=True, timeout=10)
        self.data = json.loads(self.data_path.read_text())
        if okay:
            self.assertEqual(result.returncode,0,result.stderr)
        else:
            self.assertNotEqual(result.returncode,0)
        return result

    def test_explicit_baseline_and_new_incoming(self):
        self.data["events"] = [event(1,10),event(2,11)]
        self.save()
        self.run_agent(start=None,okay=False)
        self.run_agent(start="2")
        self.assertEqual((self.data["acked"],self.data["sends"]),(2,0))
        self.data["events"].append(event(3,12))
        self.save()
        self.run_agent(start=None)
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(3,1,1))
        self.run_agent(start=None)
        self.assertEqual(self.data["sends"],1)
        self.assertEqual((self.state / "channel-agent.json").stat().st_mode & 0o777,0o600)

    def test_outgoing_update_and_nontext_skip(self):
        self.data["events"] = [event(1,10,"outgoing"),event(2,11,kind="message_updated"),
                               event(3,12,content={"Image":{"md5":None}})]
        self.save()
        self.run_agent()
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(3,0,0))

    def test_agent_failure_and_unknown_direction_do_not_ack(self):
        self.data["events"] = [event(1,10)]
        self.data["agent_fail"] = True
        self.save()
        self.run_agent(okay=False)
        self.assertEqual((self.data["acked"],self.data["sends"]),(0,0))
        self.data["agent_fail"] = False
        self.data["events"][0] = event(1,10,"unknown")
        self.save()
        self.run_agent(start=None,okay=False)
        self.assertEqual((self.data["acked"],self.data["sends"]),(0,0))

    def test_indeterminate_send_never_replayed(self):
        self.data["events"] = [event(1,10)]
        self.data["send_result"] = "indeterminate"
        self.save()
        self.run_agent(okay=False)
        self.assertEqual((self.data["acked"],self.data["sends"]),(0,1))
        memory = self.state / "agent-memory.json"
        memory.write_text("{invalid")
        memory.chmod(0o600)
        self.run_agent(start=None,okay=False)
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(0,1,1))

    def test_duplicate_server_id_does_not_regenerate(self):
        self.data["events"] = [event(1,10),event(2,10)]
        self.save()
        self.run_agent()
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(2,1,1))

    def test_exited_child_preserves_command_failure(self):
        failed = self.root / "failed-child"
        failed.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(7)\n")
        failed.chmod(0o700)
        with self.assertRaisesRegex(channel_agent.RunnerError, "command failed"):
            channel_agent.command([str(failed)], timeout=2)

    def test_read_disconnect_recovers_in_order_without_repeat(self):
        self.data["events"] = [event(1,10),event(2,11)]
        self.data["fail_before"] = {"status":1,"subscribe":2,"events":1,"list":1,"show":1}
        self.save()
        self.run_agent()
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(2,2,2))
        self.assertEqual([x["text"] for x in self.data["outbox"].values()], ["reply to 10","reply to 11"])
        health = json.loads((self.state / "channel-agent-health.json").read_text())
        self.assertEqual((health["stage"],health["last_processed_cursor"],health["exit_code"]),("stopped",2,0))
        self.assertGreaterEqual(health["retry_count"],6)
        observed = (self.state / "channel-agent-events.jsonl").read_text()
        self.assertNotIn("hello",observed)
        self.assertNotIn("test@chatroom",observed)
        self.run_agent(start=None)
        self.assertEqual((self.data["sends"],self.data["agent_calls"]),(2,2))

    def test_ack_and_queue_lost_response_are_idempotent(self):
        self.data["events"] = [event(1,10)]
        self.data["fail_after"] = {"ack":1,"queue":1}
        self.save()
        self.run_agent()
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(1,1,1))
        self.assertEqual(len(self.data["outbox"]),1)

    def test_model_transient_failures_retry_before_reply(self):
        self.data["events"] = [event(1,10)]
        self.data["agent_fail_count"] = 2
        self.save()
        self.run_agent()
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(1,1,3))
        self.assertEqual(len(self.data["outbox"]),1)

    def test_unknown_send_stops_before_later_message(self):
        self.data["events"] = [event(1,10),event(2,11)]
        self.data["send_result"] = "indeterminate"
        self.save()
        self.run_agent(okay=False)
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(0,1,1))
        self.assertEqual(json.loads((self.state / "channel-agent-health.json").read_text())["exit_code"],1)
        self.run_agent(start=None,okay=False)
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(0,1,1))

    def test_two_processes_refuse_same_lock(self):
        self.data["events"] = [event(1,10)]
        self.data["agent_sleep"] = 1.5
        self.save()
        argv = [sys.executable,str(RUNNER),"--wx-cli",str(self.cli),"--state-dir",str(self.state),
                "--consumer","test-agent","--agent-command-json",json.dumps([str(self.agent)]),
                "--sender-driver","/remote/driver","--retry-base-seconds","0.01","--once",
                "--start-after","0"]
        first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import time
            for _ in range(100):
                if self.data_path.exists() and json.loads(self.data_path.read_text()).get("agent_calls"):
                    break
                time.sleep(0.02)
            second = subprocess.run(argv, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(second.returncode,0)
            self.assertIn("lock", second.stderr.lower())
            self.assertEqual(first.wait(timeout=5),0)
        finally:
            if first.poll() is None:
                first.kill(); first.wait()
            first.stdout.close()
            first.stderr.close()

    def test_operator_stop_and_pause_are_boundary_only(self):
        control = self.state / "channel-agent-control.json"
        control.write_text(json.dumps({"version":1,"action":"stop"}))
        control.chmod(0o600)
        self.data["events"] = [event(1,10)]
        self.save()
        self.run_agent()
        self.assertEqual((self.data["acked"],self.data["sends"]),(0,0))
        health = json.loads((self.state / "channel-agent-health.json").read_text())
        self.assertEqual((health["stage"],health["terminal_reason"]),("stopped","stop"))
        control.write_text(json.dumps({"version":1,"action":"run"}))
        self.run_agent(start="0")
        self.assertEqual((self.data["acked"],self.data["sends"]),(1,1))

    def test_pause_then_run_without_consuming_during_pause(self):
        import time
        control = self.state / "channel-agent-control.json"
        control.write_text(json.dumps({"version":1,"action":"pause"}))
        control.chmod(0o600)
        self.data["events"] = [event(1,10)]
        self.save()
        argv = [sys.executable,str(RUNNER),"--wx-cli",str(self.cli),"--state-dir",str(self.state),
                "--consumer","test-agent","--agent-command-json",json.dumps([str(self.agent)]),
                "--sender-driver","/remote/driver","--retry-base-seconds","0.01","--once",
                "--start-after","0"]
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                health_path = self.state / "channel-agent-health.json"
                if health_path.exists() and json.loads(health_path.read_text()).get("stage") == "paused":
                    break
                time.sleep(0.02)
            else:
                self.fail("runner did not enter paused state")
            self.assertEqual(json.loads(self.data_path.read_text())["agent_calls"],0)
            control.write_text(json.dumps({"version":1,"action":"run"}))
            self.assertEqual(child.wait(timeout=5),0)
            result = json.loads(self.data_path.read_text())
            self.assertEqual((result["acked"],result["sends"]),(1,1))
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
            child.stdout.close()
            child.stderr.close()

    def test_unlimited_read_retries_and_bounded_exit(self):
        self.data["events"] = [event(1,10)]
        self.data["fail_before"] = {"subscribe":5}
        self.save()
        self.run_agent("--read-retries","0","--max-seconds","4")
        self.assertEqual((self.data["acked"],self.data["sends"]),(1,1))

    def test_operator_stop_interrupts_reconnect_backoff(self):
        import time
        self.data["events"] = [event(1,10)]
        self.data["fail_before"] = {"subscribe":1000}
        self.save()
        argv = [sys.executable,str(RUNNER),"--wx-cli",str(self.cli),"--state-dir",str(self.state),
                "--consumer","test-agent","--agent-command-json",json.dumps([str(self.agent)]),
                "--sender-driver","/remote/driver","--retry-base-seconds","0.05",
                "--read-retries","0","--once","--start-after","0"]
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            health_path = self.state / "channel-agent-health.json"
            for _ in range(100):
                if health_path.exists() and json.loads(health_path.read_text()).get("stage") == "reconnecting":
                    break
                time.sleep(0.02)
            else:
                self.fail("runner did not enter reconnecting state")
            control = self.state / "channel-agent-control.json"
            control.write_text(json.dumps({"version":1,"action":"stop"}))
            control.chmod(0o600)
            self.assertEqual(child.wait(timeout=5),0)
            self.assertEqual(json.loads(health_path.read_text())["terminal_reason"],"stop")
            result = json.loads(self.data_path.read_text())
            self.assertEqual((result["acked"],result["sends"]),(0,0))
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
            child.stdout.close()
            child.stderr.close()

    def test_send_lost_response_hard_stops_then_recovers_terminal(self):
        self.data["events"] = [event(1,10),event(2,11)]
        self.data["fail_after"] = {"send":1}
        self.save()
        self.run_agent(okay=False)
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(0,1,1))
        self.run_agent(start=None)
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(2,2,2))

    def test_invalid_json_is_fatal_even_with_unlimited_reads(self):
        self.data["events"] = [event(1,10)]
        self.data["bad_json"] = "subscribe"
        self.save()
        self.run_agent("--read-retries","0","--max-seconds","3",okay=False)
        self.assertEqual((self.data["acked"],self.data["sends"],self.data["agent_calls"]),(0,0,0))
        health = json.loads((self.state / "channel-agent-health.json").read_text())
        self.assertEqual((health["failure_category"],health["exit_code"]),("invalid_json",1))

    def test_binding_drift_is_fatal_on_restart(self):
        self.run_agent()
        self.data["binding"]["account"] = "different"
        self.save()
        self.run_agent(start=None,okay=False)
        self.assertEqual(json.loads((self.state / "channel-agent-health.json").read_text())["failure_category"],"binding")

    def test_resume_after_poll_delay_expired(self):
        control = self.state / "channel-agent-control.json"
        channel_agent.save(control, {"version":1,"action":"pause"})
        runner = channel_agent.Runner(Namespace(state_dir=self.state, wx_cli=self.cli))
        resumed = threading.Thread(target=lambda: (time.sleep(0.05), channel_agent.save(
            control, {"version":1,"action":"run"})))
        resumed.start()
        try:
            self.assertIsNone(runner.delay(0.01))
            self.assertEqual(json.loads(runner.health_path.read_text())["stage"], "reading")
        finally:
            resumed.join(timeout=2)

    def test_stop_during_expired_delay_still_responds(self):
        control = self.state / "channel-agent-control.json"
        channel_agent.save(control, {"version":1,"action":"pause"})
        runner = channel_agent.Runner(Namespace(state_dir=self.state, wx_cli=self.cli))
        stopped = threading.Thread(target=lambda: (time.sleep(0.05), channel_agent.save(
            control, {"version":1,"action":"stop"})))
        stopped.start()
        try:
            self.assertEqual(runner.delay(0.01), "stop")
        finally:
            stopped.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
