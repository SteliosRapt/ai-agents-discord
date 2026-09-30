import ast
import builtins
from concurrent.futures import ThreadPoolExecutor
import contextlib
import ctypes
import io
import json
import os
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import agent_state
import session_hook

class StateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = agent_state.State(self.root)
        self.addCleanup(self.state.close)

    def touch(self, session='host-1', event='SessionStart', pid=None):
        return self.state.touch_host_session('claude', session, self.root, pid, event)

    def test_stable_handle_activity_and_reopening(self):
        with patch.object(agent_state.time, 'time', return_value=100):
            first = self.touch(pid=42)
        self.assertIsInstance(first, sqlite3.Row)
        self.assertRegex(first['handle'], r'^[a-z0-9]{4}$')
        self.assertEqual((first['started_at'], first['last_seen'], first['host'], first['cwd'], first['host_pid']),
                         (100, 100, 'claude', str(self.root), 42))
        with patch.object(agent_state.time, 'time', return_value=200):
            ended = self.touch(event='SessionEnd')
        self.assertEqual(ended['ended_at'], 200)
        with patch.object(agent_state.time, 'time', return_value=300):
            again = self.state.touch_host_session('claude', 'host-1', self.root / 'other', 99, 'UserPromptSubmit')
        self.assertEqual(again['handle'], first['handle'])
        self.assertEqual(again['started_at'], 100)
        self.assertIsNone(again['ended_at'])
        self.assertEqual((again['last_seen'], again['last_event'], again['cwd'], again['host_pid']),
                         (300, 'UserPromptSubmit', str(self.root / 'other'), 99))
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM host_sessions').fetchone()[0], 1)
        self.assertEqual(self.state.host_session_by_handle(first['handle'])['host_session_id'], 'host-1')
        self.assertIsNone(self.state.host_session_by_handle('none'))

    def test_handle_collision_retries(self):
        with patch.object(agent_state.secrets, 'choice', side_effect=list('aaaaaaaaaaab')) as choice:
            first = self.touch('one')
            second = self.touch('two')
        self.assertEqual((first['handle'], second['handle']), ('aaaa', 'aaab'))
        self.assertEqual(choice.call_count, 12)

    def test_link_survives_activity(self):
        row = self.touch()
        self.state.link_host_session('host-1', 'a' * 32, '123', 'Task')
        self.touch(event='Stop')
        linked = self.state.host_session_by_handle(row['handle'])
        self.assertEqual((linked['mcp_session_id'], linked['thread_id'], linked['title']), ('a' * 32, '123', 'Task'))

    def test_live_filters_order_and_default_window(self):
        for session, seen, event, pid in [('old', 50000 - 43201, 'Stop', None),
                                          ('ended', 49999, 'SessionEnd', None),
                                          ('dead', 49998, 'Stop', 42),
                                          ('newest', 49997, 'Stop', 43),
                                          ('no-pid', 49996, 'Stop', None),
                                          ('boundary', 50000 - 43200, 'Stop', None)]:
            with patch.object(agent_state.time, 'time', return_value=seen):
                self.touch(session, event, pid)
        with patch.object(agent_state.time, 'time', return_value=50000), patch.object(agent_state, 'process_alive', side_effect=lambda pid: pid == 43) as alive:
            self.assertEqual([row['host_session_id'] for row in self.state.live_host_sessions()], ['newest', 'no-pid', 'boundary'])
            self.assertEqual([call.args[0] for call in alive.call_args_list], [42, 43])
            self.assertEqual([row['host_session_id'] for row in self.state.live_host_sessions(3)], ['newest'])
        with patch.object(agent_state.time, 'time', return_value=50000), patch.object(agent_state, 'process_alive', side_effect=RuntimeError('Cannot query process')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot query process'):
                self.state.live_host_sessions()

    def test_inbox_delivery_is_targeted_and_once(self):
        path = self.root / 'file.txt'
        with patch.object(agent_state.time, 'time', return_value=100):
            first = self.state.add_inbox('one', path, 'Read this', 'owner')
            second = self.state.add_inbox('one', path, '', 'owner')
            other = self.state.add_inbox('two', path, 'Other session', 'owner')
        with patch.object(agent_state.time, 'time', return_value=200):
            rows = self.state.take_inbox('one')
        self.assertEqual([row['id'] for row in rows], [first, second])
        self.assertEqual((rows[0]['path'], rows[0]['note'], rows[0]['sender_id'], rows[0]['created_at']),
                         (str(path), 'Read this', 'owner', 100))
        self.assertEqual(self.state.take_inbox('one'), [])
        self.assertEqual([row[0] for row in self.state.db.execute('SELECT delivered_at FROM inbox WHERE host_session_id=?', ('one',))], [200, 200])
        self.assertEqual([row['id'] for row in self.state.take_inbox('two')], [other])

    def test_inbox_concurrent_connections_do_not_duplicate(self):
        ids = [self.state.add_inbox('one', self.root / str(i), 'note', 'owner') for i in range(10)]
        other = agent_state.State(self.root)
        self.addCleanup(other.close)
        barrier = threading.Barrier(2)
        def take(state):
            barrier.wait(timeout=5)
            return [row['id'] for row in state.take_inbox('one')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(take, state) for state in (self.state, other)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(results[0] + results[1]), ids)
        self.assertEqual(sorted(len(result) for result in results), [0, 10])

    def test_inbox_failure_rolls_back(self):
        self.state.add_inbox('one', 'file', 'note', 'owner')
        self.state.db.execute("CREATE TRIGGER reject_delivery BEFORE UPDATE ON inbox BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'offline failure'):
            self.state.take_inbox('one')
        self.assertFalse(self.state.db.in_transaction)
        self.state.db.execute('DROP TRIGGER reject_delivery')
        self.assertEqual(len(self.state.take_inbox('one')), 1)

    def test_staggered_events_do_not_retry_an_active_delivery(self):
        item = self.state.add_inbox('one', self.root / 'file.txt', 'note', 'owner')
        other = agent_state.State(self.root)
        self.addCleanup(other.close)
        committed, resume = threading.Event(), threading.Event()
        db = self.state.db
        class PausedCommit:
            def execute(self, sql, *args):
                result = db.execute(sql, *args)
                if sql == 'COMMIT':
                    committed.set()
                    if not resume.wait(timeout=5):
                        raise AssertionError('first take was not resumed')
                return result
            def __getattr__(self, name):
                return getattr(db, name)
        self.state.db = PausedCommit()
        with patch.object(agent_state.time, 'time', side_effect=lambda: 100 if threading.current_thread().name.startswith('first-take') else 101), ThreadPoolExecutor(max_workers=1, thread_name_prefix='first-take') as pool:
            first = pool.submit(self.state.take_inbox, 'one', retry_before=100, event='UserPromptSubmit')
            try:
                self.assertTrue(committed.wait(timeout=5))
                self.assertFalse(first.done())
                # The second event starts later, after the first claim was committed.
                self.assertEqual(other.take_inbox('one', retry_before=101, event='UserPromptSubmit'), [])
                other.confirm_inbox('one', 'Stop', 101)
                self.assertIsNone(other.db.execute('SELECT confirmed_at FROM inbox WHERE id=?', (item,)).fetchone()[0])
            finally:
                resume.set()
            self.assertEqual([row['id'] for row in first.result(timeout=5)], [item])
        self.state.db = db
        self.assertEqual(other.take_inbox('one', retry_before=102, event='UserPromptSubmit'), [])
        with patch.object(agent_state.time, 'time', return_value=102):
            self.state.finish_inbox()
        # A delayed take from the already-overlapping event must also stay empty after release.
        self.assertEqual(other.take_inbox('one', retry_before=101, event='UserPromptSubmit'), [])
        with patch.object(agent_state.time, 'time', return_value=103):
            replay = other.take_inbox('one', retry_before=103, event='PostToolUse')
        self.assertEqual([row['id'] for row in replay], [item])
        self.assertEqual(replay[0]['delivered_at'], 102)

    def test_delivery_claims_are_released_only_by_their_owner(self):
        first = self.state.add_inbox('one', 'first.txt', 'first', 'owner')
        self.state.take_inbox('one', event='UserPromptSubmit')
        second = self.state.add_inbox('one', 'second.txt', 'second', 'owner')
        other = agent_state.State(self.root)
        self.addCleanup(other.close)
        self.assertEqual([row['id'] for row in other.take_inbox('one', event='UserPromptSubmit')], [second])
        self.state.finish_inbox()
        rows = {row['id']: row for row in self.state.db.execute('SELECT * FROM inbox')}
        self.assertIsNone(rows[first]['delivery_pid'])
        self.assertIsNone(rows[first]['delivery_owner'])
        self.assertEqual(rows[second]['delivery_pid'], os.getpid())
        self.assertIsNotNone(rows[second]['delivery_owner'])

    def test_dead_delivery_owner_is_replayed_not_prematurely_confirmed(self):
        item = self.state.add_inbox('one', 'file.txt', 'note', 'owner')
        self.state.db.execute("UPDATE inbox SET delivered_at=100, delivered_event='PostToolUse', delivery_pid=999, delivery_owner='abandoned'")
        self.state.confirm_inbox('one', 'Stop', 101)
        self.assertIsNone(self.state.db.execute('SELECT confirmed_at FROM inbox').fetchone()[0])
        with patch.object(agent_state, 'process_alive', return_value=False) as alive, patch.object(agent_state.time, 'time', return_value=101):
            rows = self.state.take_inbox('one', retry_before=101, event='PostToolUse')
        alive.assert_called_once_with(999)
        self.assertEqual([(row['id'], row['delivered_at']) for row in rows], [(item, 100)])
        with patch.object(agent_state.time, 'time', return_value=102):
            self.state.finish_inbox()
        self.state.confirm_inbox('one', 'PostToolUse', 103)
        self.assertEqual(self.state.take_inbox('one', retry_before=104, event='UserPromptSubmit'), [])

    def test_delivery_liveness_failure_rolls_back_and_raises(self):
        self.state.add_inbox('one', 'file.txt', 'note', 'owner')
        self.state.db.execute("UPDATE inbox SET delivered_at=100, delivery_pid=999, delivery_owner='abandoned'")
        with patch.object(agent_state, 'process_alive', side_effect=RuntimeError('Cannot query delivery process')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot query delivery process'):
                self.state.take_inbox('one', retry_before=101, event='UserPromptSubmit')
        self.assertFalse(self.state.db.in_transaction)
        self.assertEqual(self.state.db.execute('SELECT delivery_owner FROM inbox').fetchone()[0], 'abandoned')

    def test_failed_replay_restores_the_previous_delivery(self):
        self.state.add_inbox('one', 'file.txt', 'note', 'owner')
        self.state.db.execute("UPDATE inbox SET delivered_at=100, delivered_event='UserPromptSubmit'")
        with patch.object(agent_state.time, 'time', return_value=101):
            self.state.take_inbox('one', retry_before=101, event='PostToolUse')
        self.state.finish_inbox(delivered=False)
        row = self.state.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual((row['delivered_at'], row['delivered_event'], row['delivery_pid'], row['delivery_owner']),
                         (100, 'UserPromptSubmit', None, None))

    def test_failed_replay_restores_an_abandoned_claim_for_later_recovery(self):
        self.state.add_inbox('one', 'file.txt', 'note', 'owner')
        self.state.db.execute("UPDATE inbox SET delivered_at=100, delivered_event='PostToolUse', delivery_pid=999, delivery_owner='abandoned'")
        with patch.object(agent_state, 'process_alive', return_value=False):
            self.state.take_inbox('one', retry_before=101, event='PostToolUse')
        self.state.finish_inbox(delivered=False)
        row = self.state.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual((row['delivered_at'], row['delivered_event'], row['delivery_pid'], row['delivery_owner']),
                         (100, 'PostToolUse', 999, 'abandoned'))
        self.state.confirm_inbox('one', 'Stop', 102)
        self.assertIsNone(self.state.db.execute('SELECT confirmed_at FROM inbox').fetchone()[0])
        with patch.object(agent_state, 'process_alive', return_value=False):
            self.assertEqual(len(self.state.take_inbox('one', retry_before=102, event='PostToolUse')), 1)

    def test_phase_two_schema_migrates_existing_host_and_inbox_rows(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with contextlib.closing(sqlite3.connect(root / 'state.db')) as db:
                db.executescript('''
                    CREATE TABLE host_sessions (host_session_id TEXT PRIMARY KEY, host TEXT, handle TEXT UNIQUE,
                        cwd TEXT, host_pid INTEGER, started_at REAL, last_seen REAL, last_event TEXT, ended_at REAL,
                        mcp_session_id TEXT, thread_id TEXT, title TEXT);
                    CREATE TABLE inbox (id INTEGER PRIMARY KEY AUTOINCREMENT, host_session_id TEXT, path TEXT,
                        note TEXT, sender_id TEXT, created_at REAL, delivered_at REAL);
                    INSERT INTO host_sessions VALUES ('old-host', 'claude', 'abcd', '.', NULL, 1, 2, 'Stop', NULL, 'mcp', '123', 'Old task');
                    INSERT INTO inbox VALUES (1, 'old-host', 'pending.txt', 'pending', '2', 1, NULL);
                    INSERT INTO inbox VALUES (2, 'old-host', 'sent.txt', 'unconfirmed', '2', 1, 2);
                ''')
            for _ in range(2):
                state = agent_state.State(root)
                try:
                    row = state.host_session_by_handle('abcd')
                    self.assertEqual((row['thread_id'], row['title']), ('123', 'Old task'))
                    self.assertIsNone(row['last_prompt_at'])
                    self.assertEqual([row['confirmed_at'] for row in state.db.execute('SELECT * FROM inbox')], [None, None])
                    self.assertEqual(sum(row['name'] == 'confirmed_at' for row in state.db.execute('PRAGMA table_info(inbox)')), 1)
                finally:
                    state.close()
            state = agent_state.State(root)
            try:
                with patch.object(agent_state.time, 'time', return_value=10):
                    state.confirm_inbox('old-host', 'PostToolUse', 10)
                    rows = state.take_inbox('old-host', retry_before=10, event='PostToolUse')
                self.assertEqual([(row['id'], row['delivered_at']) for row in rows], [(1, None), (2, 2)])
                with patch.object(agent_state.time, 'time', return_value=10):
                    state.finish_inbox()
                state.confirm_inbox('old-host', 'PostToolUse', 11)
                self.assertEqual(state.take_inbox('old-host', retry_before=12, event='PostToolUse'), [])
            finally:
                state.close()

    def test_concurrent_touches_preserve_one_handle(self):
        other = agent_state.State(self.root)
        self.addCleanup(other.close)
        barrier = threading.Barrier(2)
        def touch(state):
            barrier.wait(timeout=5)
            return state.touch_host_session('codex', 'same', self.root, None, 'Stop')['handle']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(touch, state) for state in (self.state, other)]
            handles = [future.result(timeout=10) for future in futures]
        self.assertEqual(handles[0], handles[1])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM host_sessions').fetchone()[0], 1)

    def test_hosts_cli(self):
        row = self.touch()
        self.state.link_host_session('host-1', 'b' * 32, '456', 'CLI task')
        with patch.object(agent_state, 'ROOT', self.root), patch('sys.argv', ['agent_state.py', 'hosts']), contextlib.redirect_stdout(io.StringIO()) as out:
            agent_state.main()
        self.assertEqual(out.getvalue(), f'{row["handle"]} claude project={self.root.name} title=CLI task thread=456 idle=0s\n')

    def test_old_schema_migration_preserves_existing_data_and_methods(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with contextlib.closing(sqlite3.connect(root / 'state.db')) as db:
                db.executescript('''
                    CREATE TABLE sessions (id TEXT PRIMARY KEY, host TEXT, pid INTEGER, ppid INTEGER, cwd TEXT,
                        project_dir TEXT, started_at REAL, last_seen REAL, status TEXT, title TEXT, plan TEXT,
                        channel_id TEXT, thread_id TEXT, status_message_id TEXT);
                    CREATE TABLE project_channels (project_dir TEXT PRIMARY KEY, channel_id TEXT, created_at REAL);
                    CREATE TABLE questions (id TEXT PRIMARY KEY, session_id TEXT, thread_id TEXT,
                        created_at REAL, answered_message_id TEXT);
                    INSERT INTO sessions (id, host, status, title) VALUES ('old', 'claude', 'connected', 'Existing');
                    INSERT INTO project_channels VALUES ('project', '100', 1);
                    INSERT INTO questions VALUES ('question', 'old', '101', 2, NULL);
                ''')
                schema = db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                project_columns = db.execute('PRAGMA table_info(project_channels)').fetchall()
            state = agent_state.State(root)
            try:
                self.assertEqual(state.session('old')['title'], 'Existing')
                state.update('old', title='Updated', status='working')
                self.assertEqual(state.session('old')['status'], 'working')
                self.assertEqual(state.project_channel('project', Mock(side_effect=AssertionError('must reuse'))), '100')
                self.assertEqual(state.project_channel('new', lambda: {'id': '200'}), '200')
                self.assertEqual(state.db.execute('SELECT * FROM questions').fetchone()['session_id'], 'old')
                registered = state.register('codex')
                self.assertEqual((state.session(registered)['host'], state.session(registered)['pid']), ('codex', os.getpid()))
                with self.assertRaisesRegex(ValueError, 'Invalid session field'):
                    state.update(registered, handle='bad')
                for name, sql in schema:
                    if name != 'project_channels':
                        self.assertEqual(state.db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0], sql)
                migrated_columns = [tuple(row) for row in state.db.execute('PRAGMA table_info(project_channels)')]
                self.assertEqual(migrated_columns[:len(project_columns)], project_columns)
                self.assertEqual([column[1] for column in migrated_columns[len(project_columns):]], ['status', 'reserved_at'])
                project = state.db.execute("SELECT * FROM project_channels WHERE project_dir='project'").fetchone()
                self.assertEqual((project['channel_id'], project['created_at'], project['status'], project['reserved_at']),
                                 ('100', 1, 'ready', None))
                self.assertEqual(state.db.execute('PRAGMA user_version').fetchone()[0], agent_state.SCHEMA_VERSION)
                tables = {row[0] for row in state.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({'host_sessions', 'inbox'} <= tables)
                self.assertEqual([row[1] for row in state.db.execute('PRAGMA table_info(host_sessions)')],
                                 ['host_session_id', 'host', 'handle', 'cwd', 'host_pid', 'started_at', 'last_seen', 'last_event', 'ended_at', 'mcp_session_id', 'thread_id', 'title', 'last_prompt_at'])
                self.assertEqual([row[1] for row in state.db.execute('PRAGMA table_info(inbox)')],
                                 ['id', 'host_session_id', 'path', 'note', 'sender_id', 'created_at', 'delivered_at', 'confirmed_at', 'delivered_event', 'delivery_pid', 'delivery_owner'])
            finally:
                state.close()

class HookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(agent_state, 'ROOT', self.root))
        self.pid = self.enterContext(patch.object(agent_state, 'host_process_pid', return_value=42))
        self.state = agent_state.State(self.root)
        self.addCleanup(self.state.close)

    def run_hook(self, event='PostToolUse', host='claude', **fields):
        payload = {'session_id': 'host-1', 'hook_event_name': event, 'cwd': str(self.root)}
        payload.update(fields)
        return self.run_input(json.dumps(payload), host)

    def run_input(self, text, host='claude'):
        raw = text.encode('utf-8') if isinstance(text, str) else text
        with patch('sys.argv', ['session_hook.py', '--host', host]), patch('sys.stdin', io.TextIOWrapper(io.BytesIO(raw), encoding='cp1253')), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = session_hook.main()
        return code, out.getvalue(), err.getvalue()

    def test_utf8_input_ignores_console_encoding_and_rejects_invalid_bytes(self):
        cwd = str(self.root / 'Ελληνικά 📎')
        payload = {'session_id': 'utf8', 'hook_event_name': 'SessionStart', 'cwd': cwd}
        self.assertEqual(self.run_input(json.dumps(payload, ensure_ascii=False)), (0, '', ''))
        self.assertEqual(self.state.db.execute('SELECT cwd FROM host_sessions').fetchone()[0], cwd)
        self.assertEqual(self.run_input(b'{"cwd":"\xff"}'),
                         (1, '', 'Invalid hook input: expected one JSON object\n'))

    def test_inbox_exact_output_both_hosts_and_events(self):
        for host in ('claude', 'codex'):
            for event in ('PostToolUse', 'UserPromptSubmit'):
                with self.subTest(host=host, event=event):
                    session = host + '-' + event
                    paths = [self.root / 'reference.txt', self.root / 'second.txt']
                    for path, note in zip(paths, ['Please inspect', 'Owner note']):
                        self.state.add_inbox(session, path, note, 'owner')
                    self.state.add_inbox('other', self.root / 'private.txt', 'Other agent', 'owner')
                    code, out, err = self.run_hook(event, host, session_id=session)
                    expected = {'hookSpecificOutput': {'hookEventName': event, 'additionalContext':
                                'AI Agents inbox: the owner sent you these via Discord /send\n'
                                f'{paths[0]}: Please inspect\n{paths[1]}: Owner note'}}
                    self.assertEqual((code, out, err), (0, json.dumps(expected) + '\n', ''))
                    if event == 'PostToolUse':
                        self.assertEqual(self.run_hook(event, host, session_id=session), (0, '', ''))
                    else:
                        expected['hookSpecificOutput']['additionalContext'] = expected['hookSpecificOutput']['additionalContext'].replace('Please inspect', 'Please inspect (re-sent)').replace('Owner note', 'Owner note (re-sent)')
                        self.assertEqual(self.run_hook(event, host, session_id=session), (0, json.dumps(expected) + '\n', ''))
                    row = self.state.db.execute('SELECT * FROM host_sessions WHERE host_session_id=?', (session,)).fetchone()
                    self.assertEqual((row['host'], row['cwd'], row['host_pid'], row['last_event']), (host, str(self.root), 42, event))

    def test_other_events_only_touch_and_preserve_inbox(self):
        item = self.state.add_inbox('host-1', self.root / 'reference.txt', 'Read later', 'owner')
        for event in ('SessionStart', 'Stop', 'SessionEnd'):
            self.assertEqual(self.run_hook(event), (0, '', ''))
            row = self.state.db.execute('SELECT * FROM host_sessions').fetchone()
            self.assertEqual(row['last_event'], event)
            self.assertEqual(row['ended_at'] is not None, event == 'SessionEnd')
            self.assertIsNone(self.state.db.execute('SELECT delivered_at FROM inbox WHERE id=?', (item,)).fetchone()[0])
        self.assertEqual(self.pid.call_count, 3)

    def test_start_session_links_different_wrappers_and_tool_names(self):
        result = {'session_id': 'ab12' * 8, 'thread_id': '123456789'}
        responses = [result, {'result': result},
                     [{'type': 'text', 'text': json.dumps(result)}],
                     {'content': [{'type': 'text', 'text': json.dumps(result)}]},
                     {'session_id': 'ab12' * 8, 'thread_id': 123456789}]
        for tool in ('mcp__ai-agents__start_session', 'mcp__ai_agents__start_session'):
            for index, response in enumerate(responses):
                with self.subTest(tool=tool, response=response):
                    session = tool + str(index)
                    self.assertEqual(self.run_hook(session_id=session, tool_name=tool, tool_response=response, tool_input={'title': 'Title'}), (0, '', ''))
                    row = self.state.db.execute('SELECT * FROM host_sessions WHERE host_session_id=?', (session,)).fetchone()
                    self.assertEqual((row['mcp_session_id'], row['thread_id'], row['title']), ('ab12' * 8, '123456789', 'Title'))

    def test_failed_or_unrelated_tool_does_not_link(self):
        valid = {'session_id': 'a' * 32, 'thread_id': '123'}
        cases = [('mcp__ai-agents__start_session', {'error': 'failed'}),
                 ('mcp__ai-agents__start_session', {'session_id': 'a' * 31, 'thread_id': '123'}),
                 ('mcp__ai-agents__start_session', {'session_id': 'a' * 33, 'thread_id': '123'}),
                 ('mcp__ai-agents__start_session', {'session_id': 'a' * 32, 'thread_id': '123x'}),
                 ('mcp__other__start_session', valid), ('mcp__ai-agents__update_status', valid)]
        for index, (tool, response) in enumerate(cases):
            session = str(index)
            self.assertEqual(self.run_hook(session_id=session, tool_name=tool, tool_response=response), (0, '', ''))
            row = self.state.db.execute('SELECT * FROM host_sessions WHERE host_session_id=?', (session,)).fetchone()
            self.assertIsNone(row['mcp_session_id'])
        self.assertEqual(self.run_hook('Stop', tool_name='mcp__ai-agents__start_session', tool_response=valid), (0, '', ''))
        self.assertIsNone(self.state.db.execute('SELECT mcp_session_id FROM host_sessions WHERE host_session_id=?', ('host-1',)).fetchone()[0])

    def test_missing_fields_and_invalid_json(self):
        for key in ('session_id', 'hook_event_name', 'cwd'):
            payload = {'session_id': 'host-1', 'hook_event_name': 'Stop', 'cwd': str(self.root)}
            del payload[key]
            code, out, err = self.run_input(json.dumps(payload))
            self.assertEqual((code, out), (1, ''))
            self.assertIn(key, err)
        for text in ('{', '[]', 'null', '{} {}'):
            code, out, err = self.run_input(text)
            self.assertEqual((code, out), (1, ''))
            self.assertIn('expected one JSON object', err)
        self.pid.assert_not_called()
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM host_sessions').fetchone()[0], 0)

    def test_hook_imports_only_stdlib_and_agent_state(self):
        source = Path(session_hook.__file__).read_text(encoding='utf-8')
        imports = []
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module.split('.')[0])
        self.assertTrue(set(imports) <= sys.stdlib_module_names | {'agent_state', 'redaction'})
        self.assertFalse({'mcp', 'bot_client', 'discord_client'} & set(imports))

    def test_no_alert_path_never_imports_bot(self):
        original = builtins.__import__
        def import_module(name, *args, **kwargs):
            if name.split('.')[0] in ('bot_client', 'discord_client', 'mcp'):
                raise AssertionError('unexpected import: ' + name)
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=import_module):
            namespace = runpy.run_path(str(Path(session_hook.__file__)))
            for host in ('claude', 'codex'):
                for event in ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop', 'SessionEnd', 'Notification', 'PermissionRequest'):
                    payload = {'session_id': 'unlinked', 'hook_event_name': event, 'cwd': str(self.root)}
                    with patch('sys.argv', ['session_hook.py', '--host', host]), patch('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode('utf-8')), encoding='cp1253')), contextlib.redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(namespace['main'](), 0)
                    self.assertEqual(out.getvalue(), '')
            row = self.state.touch_host_session('claude', 'linked', self.root, None, 'Stop')
            self.state.link_host_session('linked', 'a' * 32, '123', 'Work')
            for event in ('SessionStart', 'PostToolUse', 'Stop', 'SessionEnd', 'PermissionRequest'):
                self.assertEqual(self.run_hook(event, session_id='linked'), (0, '', ''))
            (self.root / 'config.toml').write_text('guild_id="1"\nowner_user_id="2"\nagents_category_id="3"\nupload_roots=[]\n', encoding='utf-8')
            with patch.object(agent_state.time, 'time', return_value=100):
                self.assertEqual(self.run_hook('UserPromptSubmit', session_id='linked'), (0, '', ''))
            with patch.object(agent_state.time, 'time', return_value=101):
                self.assertEqual(self.run_hook('Stop', session_id='linked'), (0, '', ''))

    def test_prompt_delivery_replays_then_tool_delivery_confirms(self):
        for next_event in ('UserPromptSubmit', 'PostToolUse'):
            with self.subTest(next_event=next_event):
                session = 'replay-' + next_event
                item = self.state.add_inbox(session, self.root / 'file.txt', 'Read this', 'owner')
                with patch.object(agent_state.time, 'time', return_value=100):
                    code, out, err = self.run_hook('UserPromptSubmit', session_id=session)
                self.assertEqual((code, err), (0, ''))
                self.assertNotIn('(re-sent)', out)
                with patch.object(agent_state.time, 'time', return_value=200):
                    code, out, err = self.run_hook(next_event, session_id=session)
                self.assertEqual((code, err), (0, ''))
                self.assertIn('(re-sent)', json.loads(out)['hookSpecificOutput']['additionalContext'])
                with patch.object(agent_state.time, 'time', return_value=300):
                    self.assertEqual(self.run_hook('Stop', session_id=session), (0, '', ''))
                with patch.object(agent_state.time, 'time', return_value=400):
                    self.assertEqual(self.run_hook('UserPromptSubmit', session_id=session), (0, '', ''))
                self.assertEqual(self.state.db.execute('SELECT confirmed_at FROM inbox WHERE id=?', (item,)).fetchone()[0], 300)

    def test_hook_retains_delivery_claim_through_remaining_duties(self):
        self.state.add_inbox('host-1', self.root / 'file.txt', 'Read first', 'owner')
        other = agent_state.State(self.root)
        self.addCleanup(other.close)
        def remaining_duties(host, row, payload):
            self.assertIn('Read first', sys.stdout.getvalue())
            self.assertEqual(other.take_inbox('host-1', retry_before=101, event='UserPromptSubmit'), [])
            other.confirm_inbox('host-1', 'Stop', 101)
            self.assertIsNone(other.db.execute('SELECT confirmed_at FROM inbox').fetchone()[0])
            return 0
        with patch.object(agent_state.time, 'time', return_value=100), patch.object(session_hook, 'post_alert', side_effect=remaining_duties):
            self.assertEqual(self.run_hook('UserPromptSubmit')[0], 0)
        row = self.state.db.execute('SELECT * FROM inbox').fetchone()
        self.assertIsNone(row['delivery_pid'])
        self.assertIsNone(row['delivery_owner'])
        with patch.object(agent_state.time, 'time', return_value=102):
            self.assertIn('(re-sent)', self.run_hook('UserPromptSubmit')[1])

    def test_failed_stdout_restores_pending_delivery(self):
        self.state.add_inbox('host-1', self.root / 'file.txt', 'Read first', 'owner')
        with patch.object(session_hook, 'print', create=True, side_effect=OSError('offline stdout failure')):
            with self.assertRaisesRegex(OSError, 'offline stdout failure'):
                self.run_hook('PostToolUse')
        row = self.state.db.execute('SELECT * FROM inbox').fetchone()
        self.assertIsNone(row['delivered_at'])
        self.assertIsNone(row['delivery_pid'])
        self.assertIsNone(row['delivery_owner'])
        code, out, err = self.run_hook('PostToolUse')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('Read first', out)
        self.assertNotIn('(re-sent)', out)

    def test_tool_delivery_confirms_only_previous_items_on_post_or_stop(self):
        for confirm_event in ('PostToolUse', 'Stop'):
            session = 'confirm-' + confirm_event
            first = self.state.add_inbox(session, self.root / 'first.txt', 'First', 'owner')
            other = self.state.add_inbox('other-session', self.root / 'other.txt', 'Other', 'owner')
            with patch.object(agent_state.time, 'time', return_value=100):
                self.assertIn('First', self.run_hook('PostToolUse', session_id=session)[1])
            second = self.state.add_inbox(session, self.root / 'second.txt', 'Second', 'owner')
            with patch.object(agent_state.time, 'time', return_value=200):
                code, out, err = self.run_hook(confirm_event, session_id=session)
            self.assertEqual((code, err), (0, ''))
            self.assertNotIn('First', out)
            self.assertNotIn('(re-sent)', out)
            self.assertEqual('Second' in out, confirm_event == 'PostToolUse')
            rows = {row['id']: row for row in self.state.db.execute('SELECT * FROM inbox')}
            self.assertEqual(rows[first]['confirmed_at'], 200)
            self.assertIsNone(rows[second]['confirmed_at'])
            self.assertIsNone(rows[other]['confirmed_at'])
            with patch.object(agent_state.time, 'time', return_value=300):
                self.assertEqual(self.run_hook('PostToolUse', session_id=session)[1] == '', confirm_event == 'PostToolUse')

    def prepare_alert(self, host='claude', **overrides):
        import bot_client as bot
        config = {'guild_id': '1', 'owner_user_id': '2', 'agents_category_id': '3', 'upload_roots': []}
        config.update(overrides)
        (self.root / 'config.toml').write_text('\n'.join(f'{key} = {json.dumps(value)}' for key, value in config.items()), encoding='utf-8')
        self.state.touch_host_session(host, 'host-1', self.root, 42, 'SessionStart')
        self.state.link_host_session('host-1', 'a' * 32, '123', 'Opted in')
        self.enterContext(patch.object(bot, 'bot_token', return_value='offline-private-bot-token'))
        return self.enterContext(patch.object(bot.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(b'{"id":"1"}')))

    def alert_payload(self, http):
        request = http.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/channels/123/messages'))
        return json.loads(request.data)

    def test_claude_notification_truncates_and_pings_owner(self):
        http = self.prepare_alert()
        self.assertEqual(self.run_hook('Notification', message='x' * 400 + ' @everyone'), (0, '', ''))
        self.assertEqual(self.alert_payload(http), {'content': '<@2> ⏳ Needs your attention: ' + 'x' * 300,
                                                   'allowed_mentions': {'parse': [], 'users': ['2']}})
        http.assert_called_once()

    def test_codex_permission_summary_and_no_ping(self):
        http = self.prepare_alert('codex', alert_mentions_owner=False)
        tool_input = {'command': 'https://discord.com/api/webhooks/123/synthetic-secret ' + 'M' * 24 + '.' + 'a' * 6 + '.' + 'b' * 27}
        self.assertEqual(self.run_hook('PermissionRequest', 'codex', tool_name='Bash', tool_input=tool_input), (0, '', ''))
        self.assertEqual(self.alert_payload(http), {'content': '⏳ Codex is waiting for approval: Bash',
                                                   'allowed_mentions': {'parse': []}})
        http.assert_called_once()

    def test_notification_and_stop_redact_before_truncating(self):
        from redaction import redact
        http = self.prepare_alert()
        message = 'x' * 270 + ' https://discord.com/api/webhooks/123/synthetic-secret ' + 'M' * 24 + '.' + 'a' * 6 + '.' + 'b' * 27
        self.assertEqual(self.run_hook('Notification', message=message), (0, '', ''))
        self.assertEqual(self.alert_payload(http)['content'], '<@2> ⏳ Needs your attention: ' + redact(message)[:300])
        with patch.object(agent_state.time, 'time', return_value=100):
            self.run_hook('UserPromptSubmit')
        with patch.object(agent_state.time, 'time', return_value=700):
            self.assertEqual(self.run_hook('Stop', last_assistant_message=message), (0, '', ''))
        self.assertEqual(self.alert_payload(http)['content'], '<@2> ✅ Turn finished after 10 min: ' + redact(message)[:300])

    def test_only_linked_matching_host_and_events_alert(self):
        http = self.prepare_alert()
        for event, host in [('Notification', 'codex'), ('PermissionRequest', 'claude'), ('SessionStart', 'claude'),
                             ('UserPromptSubmit', 'claude'), ('PostToolUse', 'claude'), ('SessionEnd', 'claude')]:
            self.assertEqual(self.run_hook(event, host, message='Attention'), (0, '', ''))
        for event, host in [('Notification', 'claude'), ('PermissionRequest', 'codex'), ('Stop', 'claude')]:
            self.assertEqual(self.run_hook(event, host, session_id='unlinked', message='Attention'), (0, '', ''))
        http.assert_not_called()

    def test_stop_threshold_last_prompt_and_truncated_summary(self):
        for host in ('claude', 'codex'):
            http = self.prepare_alert(host)
            self.assertEqual(self.run_hook('Stop', host), (0, '', ''))
            http.assert_not_called()
            with patch.object(agent_state.time, 'time', return_value=100):
                self.assertEqual(self.run_hook('UserPromptSubmit', host), (0, '', ''))
            with patch.object(agent_state.time, 'time', return_value=699):
                self.assertEqual(self.run_hook('Stop', host), (0, '', ''))
            http.assert_not_called()
            with patch.object(agent_state.time, 'time', return_value=700):
                self.assertEqual(self.run_hook('Stop', host, last_assistant_message='s' * 400), (0, '', ''))
            self.assertEqual(self.alert_payload(http)['content'], '<@2> ✅ Turn finished after 10 min: ' + 's' * 300)
            with patch.object(agent_state.time, 'time', return_value=800):
                self.assertEqual(self.run_hook('UserPromptSubmit', host), (0, '', ''))
            with patch.object(agent_state.time, 'time', return_value=801):
                self.assertEqual(self.run_hook('Stop', host), (0, '', ''))
            self.assertEqual(http.call_count, 1)
            self.state.db.execute('UPDATE host_sessions SET last_prompt_at=NULL WHERE host_session_id=?', ('host-1',))

    def test_stop_custom_threshold_and_no_summary(self):
        http = self.prepare_alert(long_turn_minutes=2, alert_mentions_owner=False)
        with patch.object(agent_state.time, 'time', return_value=100):
            self.run_hook('UserPromptSubmit')
        with patch.object(agent_state.time, 'time', return_value=220):
            self.assertEqual(self.run_hook('Stop'), (0, '', ''))
        self.assertEqual(self.alert_payload(http), {'content': '✅ Turn finished after 2 min', 'allowed_mentions': {'parse': []}})

    def test_idle_notifications_do_not_alert(self):
        http = self.prepare_alert()
        self.assertEqual(self.run_hook('Notification', message='Claude is waiting for your input'), (0, '', ''))
        self.assertEqual(self.run_hook('Notification', message='Anything', notification_type='idle_prompt'), (0, '', ''))
        http.assert_not_called()
        self.run_hook('Notification', message='Claude needs your permission to use Bash')
        http.assert_called_once()

    def test_failed_alert_is_visible_and_never_echoes_secrets(self):
        import urllib.error
        http = self.prepare_alert()
        secret = 'offline-private-bot-token'
        http.side_effect = urllib.error.URLError(secret + ' Authorization: secret https://discord.com/api/webhooks/1/private interaction-secret')
        code, out, err = self.run_hook('Notification', message='Attention')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('could not post Discord alert', err)
        self.assertIn('delivery may be uncertain', err)
        for value in (secret, 'Authorization', 'webhooks/', 'interaction-secret'):
            self.assertNotIn(value, err)
        http.assert_called_once()
        self.assertEqual(self.state.db.execute('SELECT last_event FROM host_sessions').fetchone()[0], 'Notification')

    def test_inbox_stdout_precedes_alert_failure(self):
        http = self.prepare_alert()
        http.side_effect = OSError('offline-private-bot-token')
        item = self.state.add_inbox('host-1', self.root / 'file.txt', 'Read first', 'owner')
        post_alert = session_hook.post_alert
        def post_after_delivery(host, row, payload):
            self.assertIn('Read first', sys.stdout.getvalue())
            # Alert events do not normally deliver inbox items; force an alert to exercise duty ordering.
            return post_alert(host, row, dict(payload, hook_event_name='Notification', message='Attention'))
        with patch.object(session_hook, 'post_alert', side_effect=post_after_delivery):
            code, out, err = self.run_hook('PostToolUse')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['hookSpecificOutput']['hookEventName'], 'PostToolUse')
        self.assertIn('could not post Discord alert', err)
        self.assertNotIn('offline-private-bot-token', out + err)
        self.assertIsNotNone(self.state.db.execute('SELECT delivered_at FROM inbox WHERE id=?', (item,)).fetchone()[0])

    def test_invalid_alert_config_reports_failure_without_bot_import(self):
        self.prepare_alert(long_turn_minutes=0)
        original = builtins.__import__
        def import_module(name, *args, **kwargs):
            if name == 'bot_client':
                raise AssertionError('bot imported before config validation')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=import_module):
            code, out, err = self.run_hook('Notification', message='Attention')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('could not load alert config (ValueError)', err)

class ProcessTests(unittest.TestCase):
    def kernel(self, entries):
        kernel = Mock()
        kernel.CreateToolhelp32Snapshot.return_value = 123
        kernel.CloseHandle.return_value = 1
        kernel.GetLastError.return_value = 18
        entries = iter(entries)
        def read(handle, pointer):
            self.assertEqual(handle, 123)
            entry = ctypes.cast(pointer, ctypes.POINTER(agent_state._ProcessEntry)).contents
            self.assertEqual(entry.dwSize, ctypes.sizeof(agent_state._ProcessEntry))
            try:
                entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile = next(entries)
                return 1
            except StopIteration:
                return 0
        kernel.Process32FirstW.side_effect = read
        kernel.Process32NextW.side_effect = read
        return kernel

    def test_skips_all_shells_case_insensitively_and_defaults_pid(self):
        names = sorted(agent_state.SHELLS)
        entries = [(1, 2, 'python.exe')]
        entries += [(index + 2, index + 3, name.upper()) for index, name in enumerate(names)]
        host = len(names) + 2
        entries += [(host, 0, 'Claude.exe')]
        kernel = self.kernel(entries)
        with patch.object(agent_state.ctypes.windll, 'kernel32', kernel), patch.object(agent_state.os, 'getpid', return_value=1):
            self.assertEqual(agent_state.host_process_pid(), host)
        kernel.CreateToolhelp32Snapshot.assert_called_once_with(0x2, 0)
        kernel.CloseHandle.assert_called_once_with(123)
        self.assertNotIn('os.kill', Path(agent_state.__file__).read_text())

    def test_parent_is_ancestor_even_if_supplied_process_is_not_shell(self):
        kernel = self.kernel([(1, 2, 'hook.exe'), (2, 0, 'Codex.exe')])
        with patch.object(agent_state.ctypes.windll, 'kernel32', kernel):
            self.assertEqual(agent_state.host_process_pid(1), 2)

    def test_chain_end_missing_parent_or_empty_snapshot(self):
        for entries in ([(1, 0, 'python.exe')], [(1, 2, 'python.exe')], [], [(1, 2, 'python.exe'), (2, 0, 'cmd.exe')]):
            with self.subTest(entries=entries):
                kernel = self.kernel(entries)
                with patch.object(agent_state.ctypes.windll, 'kernel32', kernel):
                    self.assertIsNone(agent_state.host_process_pid(1))
                kernel.CloseHandle.assert_called_once_with(123)

    def test_cycle_raises_and_closes_snapshot(self):
        kernel = self.kernel([(1, 2, 'python.exe'), (2, 1, 'cmd.exe')])
        with patch.object(agent_state.ctypes.windll, 'kernel32', kernel):
            with self.assertRaisesRegex(RuntimeError, 'Cycle'):
                agent_state.host_process_pid(1)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_windows_api_failures_raise(self):
        for handle in (None, ctypes.c_void_p(-1).value):
            kernel = self.kernel([])
            kernel.CreateToolhelp32Snapshot.return_value = handle
            kernel.GetLastError.return_value = 5
            with patch.object(agent_state.ctypes.windll, 'kernel32', kernel):
                with self.assertRaisesRegex(RuntimeError, 'snapshot processes; Windows error 5'):
                    agent_state.host_process_pid(1)
            kernel.CloseHandle.assert_not_called()
        for stage in ('first', 'next', 'close'):
            kernel = self.kernel([(1, 2, 'python.exe'), (2, 0, 'Claude.exe')])
            kernel.GetLastError.return_value = 5
            if stage == 'first':
                kernel.Process32FirstW.side_effect = None
                kernel.Process32FirstW.return_value = 0
            elif stage == 'next':
                kernel.Process32NextW.side_effect = None
                kernel.Process32NextW.return_value = 0
            else:
                kernel.GetLastError.return_value = 18
                kernel.CloseHandle.return_value = 0
            with self.subTest(stage=stage), patch.object(agent_state.ctypes.windll, 'kernel32', kernel):
                with self.assertRaisesRegex(RuntimeError, 'Windows error'):
                    agent_state.host_process_pid(1)
            kernel.CloseHandle.assert_called_once_with(123)

if __name__ == '__main__':
    unittest.main()
