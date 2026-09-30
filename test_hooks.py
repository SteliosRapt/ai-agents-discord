import ast
from concurrent.futures import ThreadPoolExecutor
import contextlib
import ctypes
import io
import json
import os
from pathlib import Path
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
                    self.assertEqual(state.db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0], sql)
                tables = {row[0] for row in state.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({'host_sessions', 'inbox'} <= tables)
                self.assertEqual([row[1] for row in state.db.execute('PRAGMA table_info(host_sessions)')],
                                 ['host_session_id', 'host', 'handle', 'cwd', 'host_pid', 'started_at', 'last_seen', 'last_event', 'ended_at', 'mcp_session_id', 'thread_id', 'title'])
                self.assertEqual([row[1] for row in state.db.execute('PRAGMA table_info(inbox)')],
                                 ['id', 'host_session_id', 'path', 'note', 'sender_id', 'created_at', 'delivered_at'])
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
        with patch('sys.argv', ['session_hook.py', '--host', host]), patch('sys.stdin', io.StringIO(text)), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = session_hook.main()
        return code, out.getvalue(), err.getvalue()

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
                    self.assertEqual(self.run_hook(event, host, session_id=session), (0, '', ''))
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
        source = Path(session_hook.__file__).read_text()
        imports = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module.split('.')[0])
        self.assertTrue(set(imports) <= sys.stdlib_module_names | {'agent_state'})
        self.assertFalse({'mcp', 'bot_client', 'discord_client'} & set(imports))

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
