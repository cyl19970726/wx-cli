#!/usr/bin/env python3
"""Exercise the compiled CLI using generated SQLite only; no WeChat/key access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

GROUP = 'synthetic_group@chatroom'
TABLE = 'Msg_' + hashlib.md5(GROUP.encode()).hexdigest()


def fixture(root):
    for part in ('contact', 'session', 'message'):
        (root / part).mkdir(parents=True)
    with sqlite3.connect(root / 'contact/contact.db') as db:
        db.executescript('CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, nick_name TEXT, description TEXT, extra_buffer BLOB); CREATE TABLE chat_room (username TEXT, owner TEXT, ext_buffer BLOB);')
        db.execute('INSERT INTO contact VALUES (?, ?, ?, ?, ?, NULL)', (GROUP, '', '', 'Synthetic Group', ''))
        db.execute('INSERT INTO chat_room VALUES (?, ?, ?)', (GROUP, 'synthetic_self', b''))
    with sqlite3.connect(root / 'session/session.db') as db:
        db.executescript('CREATE TABLE SessionTable (username TEXT, sort_timestamp INTEGER, summary TEXT, last_msg_type INTEGER, last_msg_sender TEXT, last_sender_display_name TEXT);')
        db.execute('INSERT INTO SessionTable VALUES (?, 100, ?, 1, ?, ?)', (GROUP, 'synthetic', 'synthetic_member', 'Member'))
    with sqlite3.connect(root / 'message/message_0.db') as db:
        db.executescript('CREATE TABLE Timestamp (timestamp INTEGER); INSERT INTO Timestamp VALUES (1700000000); CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT); INSERT INTO Name2Id VALUES (1, "synthetic_member");')
        db.execute(f'CREATE TABLE [{TABLE}] (sort_seq INTEGER, server_id INTEGER, local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content BLOB, packed_info_data BLOB, status INTEGER)')
        for seq, mid in ((10, 90), (20, 80), (20, 85)):
            db.execute(f'INSERT INTO [{TABLE}] VALUES (?, ?, 1, 1, ?, ?, NULL, 0)', (seq, mid, 1700000000 + seq, b'synthetic_member:\nsame text'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='wx-channel-synthetic-') as temporary:
        root = Path(temporary)
        source, state = root / 'db_storage', root / 'state'
        fixture(source)
        prefix = [str(binary), 'channel', '--state-dir', str(state)]

        def call(*parts, input=None, okay=True, outbox=False):
            base = [str(binary), 'outbox', '--state-dir', str(state)] if outbox else prefix
            p = subprocess.run(base + list(parts), input=input, text=True, capture_output=True, timeout=15)
            if okay and p.returncode != 0:
                raise AssertionError(f'{parts[0]} failed: {p.stderr}')
            if not okay:
                assert p.returncode != 0, f'{parts[0]} should refuse'
                return p
            return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]

        binding = call('bind', '--account', 'synthetic', '--talker', GROUP, '--session', 'agent-test', '--source', str(source))[0]
        repeated = call('bind', '--account', 'synthetic', '--talker', GROUP, '--session', 'agent-test', '--source', str(source))[0]
        assert repeated == binding
        call('bind', '--account', 'other', '--talker', GROUP, '--session', 'agent-test', '--source', str(source), okay=False)
        before = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*.db')}
        assert call('sync')[0]['events_appended'] == 3
        events = call('events')
        assert [x['server_id'] for x in events] == [90, 80, 85], 'event chronology must not sort by server ID'
        assert all(x['channel_id'] == binding['channel_id'] and x['session_id'] == 'agent-test' and not x['history_complete'] for x in events)
        assert call('sync')[0]['events_appended'] == 0
        after = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*.db')}
        assert before == after, 'source database changed during read'
        assert call('subscribe', '--consumer', 'agent', '--once') == events
        assert call('subscribe', '--consumer', 'agent', '--once') == events, 'restart before ack must replay'
        call('ack', '--consumer', 'agent', '--cursor', '3')
        assert call('subscribe', '--consumer', 'agent', '--once') == []
        call('ack', '--consumer', 'agent', '--cursor', '2', okay=False)
        call('ack', '--consumer', 'agent', '--cursor', '99', okay=False)
        with sqlite3.connect(source / 'message/message_0.db') as db:
            db.execute(f'INSERT INTO [{TABLE}] VALUES (5, 70, 1, 1, 1700000005, ?, NULL, 0)', (b'synthetic_member:\nlate',))
        assert call('sync')[0]['events_appended'] == 1
        assert call('events', '--after', '3')[0]['server_id'] == 70
        with sqlite3.connect(source / 'message/message_0.db') as db:
            db.execute(f'UPDATE [{TABLE}] SET status=1 WHERE server_id=80')
        assert call('sync')[0]['events_appended'] == 1
        assert call('events', '--after', '4')[0]['kind'] == 'message_updated'
        with sqlite3.connect(source / 'message/message_0.db') as db:
            db.execute(f'INSERT INTO [{TABLE}] VALUES (99, 0, 1, 1, 1700000099, ?, NULL, 0)', (b'synthetic_member:\nno stable id',))
        call('sync', okay=False)
        assert call('status')[0]['latest_cursor'] == 5
        with sqlite3.connect(source / 'message/message_0.db') as db:
            db.execute(f'DELETE FROM [{TABLE}] WHERE server_id=0')
            db.executemany(f'INSERT INTO [{TABLE}] VALUES (?, ?, 1, 1, ?, ?, NULL, 0)', [(1000+i, 1000+i, 1700001000+i, b'synthetic_member:\nbacklog') for i in range(1100)])
        resumed = call('subscribe', '--consumer', 'agent', '--once')
        assert len(resumed) == 1102, 'one-shot subscription must drain beyond 1000 events'
        call('ack', '--consumer', 'agent', '--cursor', str(resumed[-1]['cursor']))
        assert call('subscribe', '--consumer', 'agent', '--once') == []

        queue = ('queue', '--request-id', 'reply-1', '--channel-id', binding['channel_id'], '--session-id', 'agent-test', '--text-stdin')
        assert call(*queue, input='synthetic reply', outbox=True)[0]['status'] == 'queued'
        assert call(*queue, input='synthetic reply', outbox=True)[0]['status'] == 'queued'
        call(*queue, input='conflicting reply', outbox=True, okay=False)
        call('queue', '--request-id', 'reply-2', '--channel-id', binding['channel_id'], '--session-id', 'wrong', '--text-stdin', input='synthetic reply', outbox=True, okay=False)
        listing = call('list', outbox=True)[0]
        assert len(listing) == 1 and listing[0]['status'] == 'queued'
        assert 'synthetic reply' not in json.dumps(listing)
        assert call('show', '--request-id', 'reply-1', outbox=True)[0]['text'] == 'synthetic reply'
        assert state.stat().st_mode & 0o777 == 0o700
        for name in ('channel.sqlite3', 'outgoing.sqlite3'):
            assert (state / name).stat().st_mode & 0o777 == 0o600
        print(json.dumps({'result':'passed','initial_messages':3,'late_message':1,'update':1,'backlog_messages':1100,'dedup':True,'restart_ack':True,'source_unchanged_during_reads':True,'outbox_only_queued':True,'real_wechat_used':False}))


if __name__ == '__main__':
    main()
