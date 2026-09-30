"""Offline listener tests; Discord, downloads and Task Scheduler are mocked."""
import ast
import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import agent_state
import ai_agents_daemon as daemon
import bot_client as bot

BOT = 'offline-private-bot-token'
INTERACTION = 'offline-private-interaction-token'
CONFIG = {'guild_id': '1', 'owner_user_id': '2'}

def interaction(command='agents', kind=2, **overrides):
    result = {'id': '100', 'token': INTERACTION, 'type': kind, 'guild_id': '1',
              'member': {'user': {'id': '2'}}, 'data': {'name': command}}
    result.update(overrides)
    return result

def send(handle, filename='data.txt', url='https://cdn.discordapp.com/attachments/data', size=4, note='Read this'):
    payload = interaction('send')
    payload['data'].update({'options': [{'name': 'agent', 'value': handle}, {'name': 'file', 'value': '9'},
                                       {'name': 'note', 'value': note}],
                            'resolved': {'attachments': {'9': {'filename': filename, 'url': url, 'size': size}}}})
    return payload

class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(agent_state, 'ROOT', self.root))
        self.state = agent_state.State()
        self.addCleanup(self.state.close)
        self.logger = daemon.Logger()
        self.logger.add_secret(BOT)
        self.client = bot.BotClient('1')
        self.calls = []

        def request(method, path, payload=None, files=(), **kwargs):
            self.calls.append((method, path, payload, kwargs))
            return {'id': '200'}

        self.request = self.enterContext(patch.object(self.client, 'request', side_effect=request))
        self.urlopen = self.enterContext(patch.object(daemon, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(b'data')))
        self.listener = daemon.Listener(CONFIG, self.client, '10', self.logger)
        self.addCleanup(self.listener.close)

    def session(self, name='host-one', host='claude', title=None, thread=None):
        row = self.state.touch_host_session(host, name, self.root / 'Project', None, 'SessionStart')
        if title is not None or thread is not None:
            self.state.link_host_session(name, 'a' * 32, thread, title)
        return self.state.host_session_by_handle(row['handle'])

    def inbox(self):
        return self.state.db.execute('SELECT * FROM inbox').fetchall()

    def test_refuses_other_users_and_guilds_without_state_or_download(self):
        for kind in (2, 4):
            for overrides in ({'guild_id': 'other'}, {'member': {'user': {'id': 'other'}}}, {'guild_id': None}):
                with self.subTest(kind=kind, overrides=overrides):
                    self.calls.clear()
                    payload = send('aaaa')
                    payload.update(overrides, type=kind)
                    with patch.object(agent_state, 'State') as state:
                        self.listener.handle(payload, background=False)
                        state.assert_not_called()
                    self.urlopen.assert_not_called()
                    self.assertEqual(len(self.calls), 1)
                    body = self.calls[0][2]
                    self.assertEqual(body['type'], 8 if kind == 4 else 4)
                    if kind == 4:
                        self.assertEqual(body['data']['choices'], [])
                    else:
                        self.assertEqual(body['data']['flags'], 64)
                        self.assertEqual(body['data']['content'], 'Only the owner can use these commands.')
                    self.assertNotIn('allowed_mentions', body)
                    self.assertEqual(self.inbox(), [])
        self.assertFalse((self.root / 'inbox').exists())

    def test_agents_empty_and_user_id_fallback(self):
        payload = interaction(user={'id': '2'})
        del payload['member']
        self.listener.handle(payload, background=False)
        self.assertEqual(self.calls[0][2], {'type': 4, 'data': {'flags': 64,
                         'content': 'No running Claude Code or Codex sessions with the plugin.',
                         'allowed_mentions': {'parse': []}}})

    def test_agents_exact_format_and_live_filtering(self):
        live = self.session(title='Build', thread='123')
        idle = self.session('idle', host='codex')
        ended = self.session('ended')
        self.state.touch_host_session('claude', 'ended', self.root, None, 'SessionEnd')
        old = self.session('old')
        dead = self.session('dead')
        now = time.time()
        self.state.db.execute('UPDATE host_sessions SET last_seen=? WHERE host_session_id=?', (now - 120, live['host_session_id']))
        self.state.db.execute('UPDATE host_sessions SET last_seen=? WHERE host_session_id=?', (now - 43201, old['host_session_id']))
        self.state.db.execute('UPDATE host_sessions SET host_pid=999 WHERE host_session_id=?', (dead['host_session_id'],))
        with patch.object(daemon.time, 'time', return_value=now), patch.object(agent_state, 'process_alive', return_value=False):
            self.listener.handle(interaction(), background=False)
        body = self.calls[0][2]
        self.assertEqual(body['type'], 4)
        self.assertEqual(body['data']['flags'], 64)
        lines = body['data']['content'].splitlines()
        self.assertIn(f'`{live["handle"]}` claude · Project · Build · active 2m ago · <#123>', lines)
        self.assertIn(f'`{idle["handle"]}` codex · Project · no Discord task · active 0m ago', lines)
        self.assertEqual(len(lines), 2)
        self.assertNotIn(ended['handle'], body['data']['content'])

    def test_agents_truncation_counts_all_omitted_rows(self):
        for index in range(30):
            self.session(str(index), title='Task ' + 'x' * 200, thread='123')
        self.listener.handle(interaction(), background=False)
        text = self.calls[0][2]['data']['content']
        self.assertLessEqual(len(text), 2000)
        lines = text.splitlines()
        match = re.fullmatch(r'…and (\d+) more', lines[-1])
        self.assertIsNotNone(match)
        self.assertEqual(len(lines) - 1 + int(match[1]), 30)
        self.assertTrue(all(line.startswith('`') for line in lines[:-1]))

    def test_autocomplete_filters_case_insensitively_caps_names_and_count(self):
        for index in range(60):
            self.session(str(index), host='codex' if index % 2 else 'claude', title='Long ' + 'x' * 200)
        payload = interaction('send', 4)
        payload['data']['options'] = [{'name': 'agent', 'value': 'CoDeX', 'focused': True}]
        self.listener.handle(payload, background=False)
        body = self.calls[0][2]
        self.assertEqual(body['type'], 8)
        choices = body['data']['choices']
        self.assertEqual(len(choices), 25)
        for choice in choices:
            self.assertLessEqual(len(choice['name']), 100)
            self.assertIn('codex', choice['name'].casefold())
            self.assertEqual(choice['value'], choice['name'].split(' · ')[0])
            self.assertRegex(choice['value'], r'^[a-z0-9]{4}$')
        self.calls.clear()
        payload['data']['options'][0]['value'] = 'no match anywhere'
        self.listener.handle(payload, background=False)
        self.assertEqual(self.calls[0][2]['data']['choices'], [])

    def test_send_defers_downloads_queues_posts_and_edits(self):
        row = self.session(title='Build', thread='123')

        def download(request, timeout):
            self.assertEqual(self.calls[0][2]['type'], 5, 'download preceded defer')
            self.assertEqual(self.calls[0][2]['data']['flags'], 64)
            self.assertEqual(timeout, 55)
            self.assertEqual(request.full_url, 'https://cdn.discordapp.com/attachments/data')
            self.assertIsNone(request.get_header('Authorization'))
            return io.BytesIO(b'data')

        self.urlopen.side_effect = download
        self.listener.handle(send(row['handle']), background=False)
        queued = self.inbox()
        self.assertEqual(len(queued), 1)
        self.assertEqual((queued[0]['host_session_id'], queued[0]['note'], queued[0]['sender_id']),
                         ('host-one', 'Read this', '2'))
        path = Path(queued[0]['path'])
        self.assertEqual(path.read_bytes(), b'data')
        self.assertEqual(path.parent, self.root / 'inbox' / row['handle'])
        self.assertRegex(path.name, r'^\d{8}-\d{6}_data\.txt$')
        self.assertEqual([c[0] for c in self.calls], ['POST', 'POST', 'PATCH'])
        self.assertEqual(self.calls[0][1], f'/interactions/100/{INTERACTION}/callback')
        self.assertEqual(self.calls[1][1], '/channels/123/messages')
        self.assertEqual(self.calls[1][2]['content'], f'📎 Owner sent `{path.name}` via /send: Read this')
        self.assertEqual(self.calls[2][1], f'/webhooks/10/{INTERACTION}/messages/@original')
        self.assertEqual(self.calls[2][2]['content'], f'Saved `{path.name}` for `{row["handle"]}` (claude · Project). The agent sees it at its next step.')
        self.assertNotIn('allowed_mentions', self.calls[0][2])
        self.assertEqual(self.calls[0][3], {'mentions': False})
        self.assertEqual(self.calls[2][2]['allowed_mentions'], {'parse': []})
        self.assertEqual(list(path.parent.glob('*.part')), [])

    def test_send_without_thread_or_note_and_numeric_collision_suffix(self):
        row = self.session()
        payload = send(row['handle'], filename=r'..\folder/CON.txt', url='https://media.discordapp.net/data', size=None, note='')
        fixed = Mock()
        fixed.now.return_value.strftime.return_value = '20260930-120000_'
        with patch.object(daemon, 'datetime', fixed):
            self.listener.handle(payload, background=False)
            self.listener.handle(payload, background=False)
        paths = [Path(r['path']) for r in self.inbox()]
        self.assertEqual([p.name for p in paths], ['20260930-120000__CON.txt', '20260930-120000__CON_1.txt'])
        self.assertEqual([c[0] for c in self.calls], ['POST', 'PATCH', 'POST', 'PATCH'])
        self.assertTrue(all(p.read_bytes() == b'data' for p in paths))

    def test_unknown_and_inactive_sessions_do_not_download(self):
        row = self.session()
        self.state.touch_host_session('claude', 'host-one', self.root, None, 'SessionEnd')
        for handle in ('xxxx', row['handle']):
            self.calls.clear()
            self.listener.handle(send(handle), background=False)
            self.assertEqual(self.calls[0][2]['type'], 5)
            self.assertEqual(self.calls[1][2]['content'], f'Unknown or inactive session `{handle}`. Use /agents.')
        self.urlopen.assert_not_called()
        self.assertEqual(self.inbox(), [])

    def test_send_bad_hosts_schemes_and_credentials_fail_without_download(self):
        row = self.session()
        for url in ('http://cdn.discordapp.com/file', 'https://evil.example/file', 'https://cdn.discordapp.com.evil.example/file',
                    'https://cdn.discordapp.com@evil.example/file', 'https://user:password@cdn.discordapp.com/file'):
            with self.subTest(url=url):
                self.calls.clear()
                self.listener.handle(send(row['handle'], url=url), background=False)
                self.assertEqual(self.calls[0][2]['type'], 5)
                self.assertTrue(self.calls[-1][2]['content'].startswith('Failed: ValueError:'))
                self.assertEqual(self.inbox(), [])
                self.urlopen.assert_not_called()
        self.assertFalse((self.root / 'inbox').exists())

    def test_download_errors_and_size_mismatch_remove_parts_and_add_no_inbox(self):
        row = self.session()
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.read = Mock(side_effect=[b'da', OSError(BOT + ' ' + INTERACTION)])
        for source, size in ((io.BytesIO(b'data'), 5), (urllib.error.URLError(INTERACTION), 4), (broken, 4)):
            with self.subTest(source=type(source).__name__):
                self.calls.clear()
                self.urlopen.side_effect = source if isinstance(source, Exception) else lambda *a, result=source, **k: result
                self.listener.handle(send(row['handle'], size=size), background=False)
                self.assertTrue(self.calls[-1][2]['content'].startswith('Failed: '))
                self.assertNotIn(BOT, self.calls[-1][2]['content'])
                self.assertNotIn(INTERACTION, self.calls[-1][2]['content'])
                self.assertEqual(self.inbox(), [])
                self.assertEqual(list((self.root / 'inbox' / row['handle']).iterdir()), [])
        log = self.logger.path.read_text(encoding='utf-8')
        self.assertNotIn(BOT, log)
        self.assertNotIn(INTERACTION, log)

    def test_inbox_write_failure_removes_completed_download(self):
        row = self.session()
        with patch.object(agent_state.State, 'add_inbox', side_effect=RuntimeError(INTERACTION)):
            self.listener.handle(send(row['handle']), background=False)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(list((self.root / 'inbox' / row['handle']).iterdir()), [])
        self.assertIn('Failed: RuntimeError: [redacted]', self.calls[-1][2]['content'])

    def test_thread_post_failure_is_reported_and_preserves_queued_file(self):
        row = self.session(thread='123')
        with patch.object(self.client, 'send_message', side_effect=RuntimeError(BOT + ' ' + INTERACTION)):
            self.listener.handle(send(row['handle']), background=False)
        self.assertEqual(len(self.inbox()), 1)
        self.assertTrue(Path(self.inbox()[0]['path']).is_file())
        self.assertTrue(self.calls[-1][2]['content'].startswith('Failed: RuntimeError:'))
        self.assertNotIn(BOT, self.logger.path.read_text(encoding='utf-8'))
        self.assertNotIn(INTERACTION, self.logger.path.read_text(encoding='utf-8'))

    def test_only_interaction_dispatch_is_processed_and_download_runs_off_gateway_thread(self):
        row = self.session()
        entered, release = threading.Event(), threading.Event()
        caller = threading.get_ident()
        worker_ids = []

        def download(*args, **kwargs):
            worker_ids.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test download remained blocked')
            return io.BytesIO(b'data')

        self.urlopen.side_effect = download
        self.listener.on_dispatch('MESSAGE_CREATE', {})
        self.assertEqual(self.calls, [])
        try:
            self.listener.on_dispatch('INTERACTION_CREATE', send(row['handle']))
            self.assertTrue(entered.wait(1))
            self.assertNotEqual(worker_ids, [caller])
            self.assertEqual(self.inbox(), [])
        finally:
            release.set()
            self.listener.close()
        self.assertEqual(len(self.inbox()), 1)

    def test_busy_download_pool_does_not_delay_acknowledgements_and_caps_admission(self):
        row = self.session()
        release = threading.Event()
        entered = threading.Barrier(5)
        caller = threading.get_ident()
        callback_threads = []
        original = self.request.side_effect

        def request(method, path, payload=None, **kwargs):
            if method == 'POST':
                callback_threads.append(threading.get_ident())
            return original(method, path, payload, **kwargs)

        def blocked(*args, **kwargs):
            entered.wait(timeout=3)
            if not release.wait(5):
                raise TimeoutError('test download remained blocked')
            return io.BytesIO(b'data')

        self.request.side_effect = request
        self.urlopen.side_effect = blocked
        try:
            for _ in range(4):
                self.listener.on_dispatch('INTERACTION_CREATE', send(row['handle']))
            entered.wait(timeout=3)
            start = len(self.calls)
            self.listener.on_dispatch('INTERACTION_CREATE', interaction())
            autocomplete = interaction('send', 4)
            autocomplete['data']['options'] = [{'name': 'agent', 'focused': True, 'value': ''}]
            self.listener.on_dispatch('INTERACTION_CREATE', autocomplete)
            for _ in range(4):
                self.listener.on_dispatch('INTERACTION_CREATE', send(row['handle']))
            self.listener.on_dispatch('INTERACTION_CREATE', send(row['handle']))
            bodies = [call[2] for call in self.calls[start:]]
            self.assertEqual([body['type'] for body in bodies], [4, 8, 5, 5, 5, 5, 4])
            self.assertEqual(bodies[-1]['data']['content'], 'Busy, try again shortly')
            self.assertEqual(bodies[-1]['data']['flags'], 64)
            self.assertEqual(callback_threads, [caller] * 11)
        finally:
            # Queued downloads should finish without waiting on the initial barrier.
            self.urlopen.side_effect = lambda *a, **k: io.BytesIO(b'data')
            release.set()
            self.listener.pool.shutdown(wait=True)
            self.listener.close()
        self.assertEqual(len(self.inbox()), 8)

    def test_session_ends_or_process_dies_during_download_is_not_queued(self):
        for death in ('ended', 'dead pid'):
            with self.subTest(death=death):
                row = self.session(death, thread='123')
                def downloaded(*args, **kwargs):
                    if death == 'ended':
                        self.state.touch_host_session('claude', death, self.root, None, 'SessionEnd')
                    else:
                        self.state.db.execute('UPDATE host_sessions SET host_pid=999 WHERE host_session_id=?', (death,))
                    return io.BytesIO(b'data')
                self.urlopen.side_effect = downloaded
                with patch.object(agent_state, 'process_alive', return_value=False):
                    self.listener.handle(send(row['handle']), background=False)
                self.assertEqual(self.inbox(), [])
                self.assertEqual(list((self.root / 'inbox' / row['handle']).iterdir()), [])
                self.assertEqual(self.calls[-1][2]['content'],
                                 f'Session `{row["handle"]}` ended during the download; file not queued.')
                self.assertFalse(any(call[1] == '/channels/123/messages' for call in self.calls))

    def test_liveness_recheck_and_insert_share_immediate_transaction(self):
        row = self.session()
        self.state.db.execute('UPDATE host_sessions SET host_pid=999')
        statements = []
        original = agent_state.State.add_inbox
        worker_state = []
        factory = agent_state.State

        def open_state():
            state = factory()
            state.db.set_trace_callback(statements.append)
            worker_state.append(state)
            return state

        def alive(pid):
            self.assertEqual(pid, 999)
            # The first lookup precedes the download; the second must hold the transaction.
            if len(checks) == 1:
                self.assertTrue(worker_state[0].db.in_transaction)
            checks.append(pid)
            return True

        def insert(state, *args):
            self.assertTrue(state.db.in_transaction)
            return original(state, *args)

        checks = []
        with patch.object(agent_state, 'process_alive', side_effect=alive), \
                patch.object(agent_state.State, 'add_inbox', autospec=True, side_effect=insert), \
                patch.object(agent_state, 'State', side_effect=open_state):
            self.listener.handle(send(row['handle']), background=False)
        self.assertEqual(checks, [999, 999])
        begin = statements.index('BEGIN IMMEDIATE')
        self.assertTrue(statements[begin + 1].startswith('SELECT * FROM host_sessions WHERE host_session_id='))
        self.assertTrue(statements[begin + 2].startswith('INSERT INTO inbox '))
        self.assertEqual(statements[begin + 3], 'COMMIT')
        self.assertEqual(len(self.inbox()), 1)

    def test_admission_released_after_callback_or_submission_failure(self):
        row = self.session()
        for failure in ('callback', 'submission'):
            with self.subTest(failure=failure):
                if failure == 'callback':
                    context = patch.object(self.client, 'request', side_effect=RuntimeError(INTERACTION))
                else:
                    context = patch.object(self.listener.pool, 'submit', side_effect=RuntimeError(INTERACTION))
                with context:
                    self.listener.on_dispatch('INTERACTION_CREATE', send(row['handle']))
                # All eight slots must remain available after an unsubmitted download.
                acquired = []
                try:
                    for _ in range(8):
                        acquired.append(self.listener.download_slots.acquire(blocking=False))
                    self.assertEqual(acquired, [True] * 8)
                finally:
                    for success in acquired:
                        if success:
                            self.listener.download_slots.release()

    def test_autocomplete_state_failure_uses_empty_autocomplete_and_logs_error(self):
        payload = interaction('send', 4)
        payload['data']['options'] = [{'name': 'agent', 'value': '', 'focused': True}]
        with patch.object(agent_state, 'State', side_effect=RuntimeError(INTERACTION)):
            self.listener.handle(payload, background=False)
        self.assertEqual(self.calls[0][2]['type'], 8)
        self.assertEqual(self.calls[0][2]['data']['choices'], [])
        self.assertIn('RuntimeError: [redacted]', self.logger.path.read_text(encoding='utf-8'))

    def test_callback_failure_is_logged_without_reposting_or_exposing_tokens(self):
        self.request.side_effect = RuntimeError(BOT + ' ' + INTERACTION)
        self.listener.handle(interaction(), background=False)
        self.request.assert_called_once()
        text = self.logger.path.read_text(encoding='utf-8')
        self.assertIn('Failed: RuntimeError:', text)
        self.assertNotIn(BOT, text)
        self.assertNotIn(INTERACTION, text)

    def test_unexpected_worker_failure_is_observed_and_redacted(self):
        with patch.object(self.listener, 'handle', side_effect=RuntimeError(BOT + ' ' + INTERACTION)):
            self.listener.on_dispatch('INTERACTION_CREATE', interaction())
            self.listener.close()
        text = self.logger.path.read_text(encoding='utf-8')
        self.assertIn('Interaction dispatch failed: RuntimeError:', text)
        self.assertNotIn(BOT, text)
        self.assertNotIn(INTERACTION, text)

class UtilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(agent_state, 'ROOT', self.root))

    def test_safe_names_traversal_reserved_names_characters_and_extension(self):
        for source, expected in [(r'..\elsewhere/../../CON.txt', '_CON.txt'), ('/dir/.   ', 'file'),
                                 ('á:bad?.txt', '__bad_.txt'), (' NUL.png ', '_NUL.png'), ('LPT9', '_LPT9'), ('CON .txt', '_CON .txt')]:
            with self.subTest(source=source):
                self.assertEqual(daemon.safe_name(source), expected)
        for name in ('CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))):
            self.assertEqual(daemon.safe_name(name.lower() + '.txt'), '_' + name.lower() + '.txt')
        long = daemon.safe_name('x' * 200 + '.blend')
        self.assertEqual(len(long), 120)
        self.assertTrue(long.endswith('.blend'))

    def test_redirects_are_validated_before_following(self):
        handler = daemon.AttachmentRedirect()
        request = urllib.request.Request('https://cdn.discordapp.com/first')
        with self.assertRaises(ValueError):
            handler.redirect_request(request, None, 302, 'Found', {}, 'https://evil.example/next')
        redirected = handler.redirect_request(request, None, 302, 'Found', {}, 'https://media.discordapp.net/next')
        self.assertEqual(redirected.full_url, 'https://media.discordapp.net/next')
        self.assertIsNone(redirected.get_header('Authorization'))
        with patch.object(daemon.urllib.request, 'build_opener') as build:
            daemon.urlopen(request, timeout=55)
            self.assertIsInstance(build.call_args.args[0], daemon.AttachmentRedirect)
            build.return_value.open.assert_called_once_with(request, timeout=55)

    def test_download_rejects_handle_traversal_before_opening_url(self):
        with patch.object(daemon, 'urlopen') as opening:
            with self.assertRaises(ValueError):
                daemon.download({'url': 'https://cdn.discordapp.com/file'}, '../outside')
            opening.assert_not_called()
        self.assertFalse((self.root / 'inbox').exists())

    def test_single_instance_lock_and_reacquisition(self):
        with daemon.DaemonLock():
            with self.assertRaisesRegex(RuntimeError, '^Listener already running$'):
                with daemon.DaemonLock():
                    self.fail('second lock acquired')
        with daemon.DaemonLock():
            pass

    def test_timestamp_rotation_and_all_registered_secrets_redacted(self):
        logger = daemon.Logger()
        logger.add_secret(BOT)
        logger.add_secret(INTERACTION)
        logger.path.write_bytes(b'x' * (1024 * 1024))
        logger(BOT + ' ' + INTERACTION + ' Authorization: Bot ' + BOT + '\nnext')
        self.assertEqual(logger.path.with_name('daemon.log.1').stat().st_size, 1024 * 1024)
        line = logger.path.read_text(encoding='utf-8')
        self.assertRegex(line, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC ')
        self.assertNotIn(BOT, line)
        self.assertNotIn(INTERACTION, line)
        self.assertNotIn('Authorization', line)
        self.assertEqual(len(line.splitlines()), 1)

    def test_bot_request_callback_and_registration_opt_out_default_unchanged(self):
        bodies = []

        def opening(request, timeout):
            bodies.append(json.loads(request.data))
            return io.BytesIO(b'{}')

        client = bot.BotClient('1')
        envelope = {'type': 4, 'data': {'flags': 64, 'content': 'hello', 'allowed_mentions': {'parse': []}}}
        with patch.object(bot, 'bot_token', return_value=BOT), patch.object(bot.urllib.request, 'urlopen', side_effect=opening):
            client.request('POST', '/interactions/1/token/callback', envelope, mentions=False)
            client.request('PUT', '/commands', daemon.COMMANDS, mentions=False)
            client.request('POST', '/channels/1/messages', {'content': 'hello'})
            with self.assertRaises(ValueError):
                client.request('POST', '/channels/1/messages', {'content': 'x' * 2001})
            client.request('POST', '/callback', {'content': 'x' * 2001}, mentions=False)
        self.assertEqual(bodies[0], envelope)
        self.assertEqual(bodies[1], daemon.COMMANDS)
        self.assertEqual(bodies[2], {'content': 'hello', 'allowed_mentions': {'parse': []}})
        self.assertNotIn('allowed_mentions', bodies[3])

    def test_startup_exact_guild_command_registration_and_gateway_intents(self):
        expected = [
            {'name': 'agents', 'type': 1, 'description': 'List running Claude Code / Codex sessions', 'default_member_permissions': '0', 'contexts': [0]},
            {'name': 'send', 'type': 1, 'description': 'Send a file to one running agent session', 'default_member_permissions': '0', 'contexts': [0], 'options': [
                {'type': 3, 'name': 'agent', 'description': 'Session handle (see /agents)', 'required': True, 'autocomplete': True},
                {'type': 11, 'name': 'file', 'description': 'File for the agent', 'required': True},
                {'type': 3, 'name': 'note', 'description': 'What the agent should do with it', 'required': False, 'max_length': 1000}]}]
        client = Mock()
        client.request.side_effect = [{'id': '10'}, [], {'url': 'wss://offline.gateway'}]
        with patch.object(daemon, 'bot_token', return_value=BOT), patch.object(agent_state, 'load_config', return_value=CONFIG), \
                patch.object(daemon, 'BotClient', return_value=client) as factory, patch.object(daemon, 'Gateway') as gateway, \
                patch.object(daemon, 'shutdown_signals', side_effect=lambda stop: contextlib.nullcontext()):
            self.assertEqual(daemon.run_listener(), 0)
        factory.assert_called_once_with('1')
        self.assertEqual(client.request.call_args_list[0].args, ('GET', '/applications/@me'))
        self.assertEqual(client.request.call_args_list[1].args, ('PUT', '/applications/10/guilds/1/commands', expected))
        self.assertEqual(client.request.call_args_list[1].kwargs, {'mentions': False})
        self.assertEqual(client.request.call_args_list[2].args, ('GET', '/gateway/bot'))
        self.assertEqual(gateway.call_args.args[0], BOT)
        self.assertEqual(gateway.call_args.args[2], 'wss://offline.gateway')
        self.assertEqual(gateway.call_args.kwargs['intents'], 0)
        self.assertTrue(callable(gateway.call_args.args[1]))
        self.assertTrue(gateway.return_value.run.call_args.args[0].is_set())

    def test_gateway_fatal_and_keyboard_interrupt_exit_codes_and_redaction(self):
        for error, code in ((daemon.GatewayFatal(BOT), 1), (KeyboardInterrupt(), 0)):
            with self.subTest(error=type(error).__name__):
                client = Mock()
                client.request.side_effect = [{'id': '10'}, [], {'url': 'wss://offline.gateway'}]
                with patch.object(daemon, 'bot_token', return_value=BOT), patch.object(agent_state, 'load_config', return_value=CONFIG), \
                        patch.object(daemon, 'BotClient', return_value=client), patch.object(daemon, 'Gateway') as gateway, \
                        patch.object(daemon, 'shutdown_signals', side_effect=lambda stop: contextlib.nullcontext()):
                    gateway.return_value.run.side_effect = error
                    self.assertEqual(daemon.run_listener(), code)
                    self.assertTrue(gateway.return_value.run.call_args.args[0].is_set())
        self.assertNotIn(BOT, (self.root / 'daemon.log').read_text(encoding='utf-8'))

    def test_console_close_and_ctrl_c_set_stop_and_restore_handlers(self):
        stop = threading.Event()
        kernel = Mock()
        kernel.SetConsoleCtrlHandler.return_value = 1
        with patch.object(daemon.ctypes, 'windll', Mock(kernel32=kernel)), patch.object(daemon.signal, 'signal', return_value='old') as signals:
            with daemon.shutdown_signals(stop):
                handler = kernel.SetConsoleCtrlHandler.call_args.args[0]
                self.assertEqual(handler(2), 1)
                self.assertTrue(stop.is_set())
                stop.clear()
                signals.call_args_list[0].args[1](2, None)
                self.assertTrue(stop.is_set())
                self.assertEqual(handler(999), 0)
        self.assertEqual(kernel.SetConsoleCtrlHandler.call_args_list[-1].args[1], 0)
        self.assertTrue(all(c.args[1] == 'old' for c in signals.call_args_list[-3:]))

    def test_pythonw_without_console_and_console_registration_failure(self):
        kernel = Mock()
        kernel.GetConsoleWindow.return_value = None
        with patch.object(daemon.ctypes, 'windll', Mock(kernel32=kernel)), patch.object(daemon.signal, 'signal', return_value='old'):
            with daemon.shutdown_signals(threading.Event()):
                pass
        kernel.SetConsoleCtrlHandler.assert_not_called()
        kernel.GetConsoleWindow.return_value = 1
        kernel.SetConsoleCtrlHandler.return_value = 0
        with patch.object(daemon.ctypes, 'windll', Mock(kernel32=kernel)), patch.object(daemon.signal, 'signal', return_value='old') as signals:
            with self.assertRaisesRegex(RuntimeError, 'Cannot register Windows console close handler'):
                with daemon.shutdown_signals(threading.Event()):
                    pass
        self.assertTrue(all(c.args[1] == 'old' for c in signals.call_args_list[-3:]))

    def test_install_status_and_uninstall_commands_are_mocked_and_hidden(self):
        folder = self.root / 'Python Folder'
        folder.mkdir()
        executable = folder / 'python.exe'
        pythonw = folder / 'pythonw.exe'
        pythonw.write_bytes(b'')
        result = subprocess.CompletedProcess([], 0, stdout='task exists', stderr='')
        output = io.StringIO()
        with patch.object(daemon.sys, 'executable', str(executable)), patch.object(daemon.subprocess, 'run', return_value=result) as run, contextlib.redirect_stdout(output):
            self.assertEqual(daemon.install_autostart(), 0)
            command = ['schtasks', '/Create', '/TN', daemon.TASK, '/SC', 'ONLOGON', '/RL', 'LIMITED', '/F', '/TR',
                       f'"{pythonw}" "{Path(daemon.__file__).resolve()}" run']
            run.assert_called_once_with(command, capture_output=True, encoding='oem', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
            logger = daemon.Logger()
            for index in range(6):
                logger(f'line-{index}')
            with daemon.DaemonLock():
                self.assertEqual(daemon.status(), 0)
            self.assertEqual(run.call_args.args[0], ['schtasks', '/Query', '/TN', daemon.TASK])
            self.assertEqual(daemon.uninstall_autostart(), 0)
            self.assertEqual(run.call_args.args[0], ['schtasks', '/Delete', '/TN', daemon.TASK, '/F'])
        text = output.getvalue()
        self.assertIn('Created ' + daemon.TASK, text)
        self.assertIn('Listener running', text)
        self.assertNotIn('line-0', text)
        self.assertTrue(all(f'line-{i}' in text for i in range(1, 6)))

    def test_task_errors_and_missing_pythonw_are_reported(self):
        with patch.object(daemon.sys, 'executable', str(self.root / 'python.exe')), patch.object(daemon.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'pythonw.exe is missing'):
                daemon.install_autostart()
            run.assert_not_called()
        with patch.object(daemon.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')):
            with self.assertRaisesRegex(RuntimeError, 'Task deletion failed: denied'):
                daemon.uninstall_autostart()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(daemon.status(), 0)
            self.assertIn('Listener not running', output.getvalue())
            self.assertIn('Task query exit code: 1', output.getvalue())

    def test_cli_routes_all_actions_and_only_standard_library_imports(self):
        for action, function in [('run', 'run_listener'), ('status', 'status'), ('install-autostart', 'install_autostart'), ('uninstall-autostart', 'uninstall_autostart')]:
            with patch.object(daemon, function, return_value=0) as call:
                self.assertEqual(daemon.main([action]), 0)
                call.assert_called_once_with()
        tree = ast.parse(Path(daemon.__file__).read_text(encoding='utf-8'))
        imports = {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports |= {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertLessEqual(imports, sys.stdlib_module_names | {'agent_state', 'bot_client', 'discord_gateway'})

if __name__ == '__main__':
    unittest.main()
