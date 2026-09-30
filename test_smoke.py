"""Offline smoke-runner tests: HTTP, shell processes and MCP peers are mocked."""
import asyncio
from contextlib import asynccontextmanager, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error
from mcp.client.stdio import stdio_client as sdk_stdio_client

import agent_state
import bot_client
import smoke_test as smoke

TOKEN = 'offline-private-bot-token'
HOOK = 'https://discord.com/api/webhooks/123/offline-webhook-secret'


def write_config(root):
    (root / 'config.toml').write_text('guild_id="1"\nowner_user_id="2"\nagents_category_id="3"\n'
                                    'upload_roots=[]\nread_channel_ids=["4","5"]\n', encoding='utf-8')


class RootTests(unittest.TestCase):
    def test_environment_override_reload(self):
        original = agent_state.ROOT
        try:
            for value in (None, '', 'isolated-Ελληνικά-😀'):
                with self.subTest(value=value), patch.dict(os.environ):
                    if value is None:
                        os.environ.pop('AI_AGENTS_HOME', None)
                    else:
                        os.environ['AI_AGENTS_HOME'] = value
                    importlib.reload(agent_state)
                    expected = Path(value) if value else Path.home() / '.ai-agents'
                    self.assertEqual(agent_state.ROOT, expected)
        finally:
            importlib.reload(agent_state)
            agent_state.ROOT = original


class SmokeTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        write_config(self.root)
        self.enterContext(patch.object(agent_state, 'ROOT', self.root))
        self.enterContext(patch.object(bot_client, 'bot_token', return_value=TOKEN))
        self.http_calls = []
        self.http = self.enterContext(patch.object(bot_client.urllib.request, 'urlopen', side_effect=self.request))
        self.hook_runs = []
        self.process = self.enterContext(patch.object(smoke.subprocess, 'run', side_effect=self.hook_process))
        self.enterContext(patch.object(smoke.shutil, 'which', side_effect=lambda name: name))
        self.listener = self.enterContext(patch.object(smoke, 'check_listener', return_value='lock held; Gateway ready'))
        self.mcp_runs = []
        self.session = AsyncMock()
        self.session.__aenter__.return_value = self.session
        self.session.list_tools.return_value = SimpleNamespace(tools=[SimpleNamespace(name=name) for name in smoke.TOOLS])
        self.client = self.enterContext(patch('mcp.ClientSession', return_value=self.session))
        self.transport = self.enterContext(patch('mcp.client.stdio.stdio_client', side_effect=self.mcp_transport))

    def request(self, request, **kwargs):
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Authorization'), 'Bot ' + TOKEN)
        path = request.full_url.removeprefix(bot_client.API)
        self.http_calls.append(path)
        if path == '/applications/@me':
            result = {'id': '9', 'flags': 1 << 18, 'bot_public': False}
        elif path == '/applications/9/guilds/1/commands':
            result = [{'name': 'agents'}, {'name': 'send'}]
        else:
            result = {'id': '1'}
        return io.BytesIO(json.dumps(result).encode('utf-8'))

    def hook_process(self, argv, **kwargs):
        root = Path(kwargs['env']['AI_AGENTS_HOME'])
        self.assertNotEqual(root, self.root)
        self.assertEqual((root / 'config.toml').read_bytes(), (self.root / 'config.toml').read_bytes())
        self.assertIsInstance(kwargs['input'], bytes)
        payload = json.loads(kwargs['input'].decode('utf-8'))
        self.assertIn('Ελληνικά', payload['cwd'])
        self.assertIn('😀', payload['cwd'])
        self.assertFalse((root / 'state.db').exists())
        command = argv if isinstance(argv, str) else argv[-1]
        host = 'codex' if command.endswith('--host codex') else 'claude'
        state = agent_state.State(root)
        try:
            row = state.touch_host_session(host, payload['session_id'], payload['cwd'], None, payload['hook_event_name'])
            self.assertIsNone(row['thread_id'])
        finally:
            state.close()
        self.hook_runs.append((argv, kwargs, root, payload))
        # Child output is untrusted, even on successful completion.
        return subprocess.CompletedProcess(argv, 0, TOKEN.encode(), HOOK.encode())

    @asynccontextmanager
    async def mcp_transport(self, params, errlog):
        import mcp.client.stdio as stdio
        self.assertIsNotNone(errlog)
        errlog.write(TOKEN + HOOK)
        self.mcp_runs.append((params, stdio.get_default_environment()))
        yield Mock(), Mock()

    def run_main(self, argv=None):
        with redirect_stdout(io.StringIO()) as output:
            code = smoke.main(argv or [])
        text = output.getvalue()
        for secret in (TOKEN, HOOK, 'offline-webhook-secret'):
            self.assertNotIn(secret, text)
        return code, text.splitlines()

    def test_all_pass_get_only_and_exact_entry_points(self):
        code, lines = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual([line.split(':')[0] for line in lines],
                         ['PASS ' + name for name in ('config', 'token', 'intent', 'access', 'commands',
                                                     'mcp-claude', 'mcp-codex', 'hooks', 'listener')])
        self.assertEqual(self.http_calls, ['/users/@me', '/applications/@me', '/guilds/1',
                                          '/channels/3', '/channels/4', '/channels/5',
                                          '/applications/9/guilds/1/commands'])
        self.assertEqual(self.session.initialize.await_count, 2)
        self.assertEqual(self.session.list_tools.await_count, 2)
        self.session.call_tool.assert_not_called()
        self.assertEqual(len(self.hook_runs), 30)
        self.assertFalse((self.root / 'state.db').exists())
        for _, _, root, _ in self.hook_runs:
            self.assertFalse(root.exists())

    def test_offline_only_skips_all_network_and_listener(self):
        self.http.side_effect = AssertionError('network used in offline mode')
        code, lines = self.run_main(['--offline-only'])
        self.assertEqual(code, 0)
        self.assertEqual([line.split(':')[0] for line in lines],
                         ['PASS config', 'PASS mcp-claude', 'PASS mcp-codex', 'PASS hooks'])
        self.http.assert_not_called()
        bot_client.bot_token.assert_not_called()
        self.listener.assert_not_called()

    def test_failure_reports_without_secrets_and_checks_continue(self):
        self.http.side_effect = urllib.error.URLError(TOKEN + HOOK)
        self.session.initialize.side_effect = RuntimeError(TOKEN + HOOK)
        code, lines = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 9)
        for name in ('token', 'intent', 'access', 'commands', 'mcp-claude', 'mcp-codex'):
            self.assertTrue(any(line.startswith('FAIL ' + name + ':') for line in lines))
        self.assertTrue(lines[-2].startswith('PASS hooks:'))
        self.assertTrue(lines[-1].startswith('PASS listener:'))

    def test_missing_config_fails_but_local_checks_continue(self):
        (self.root / 'config.toml').unlink()
        code, lines = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith('FAIL config:'))
        self.assertTrue(lines[-2].startswith('FAIL hooks:'))
        self.http.assert_not_called()
        self.assertEqual(len(self.mcp_runs), 2)

    def test_intent_flags_and_private_bot(self):
        runner = smoke.Smoke()
        runner.check_config()
        for flags, public, succeeds in ((1 << 18, False, True), (1 << 19, False, True),
                                       (0, False, False), (1 << 18, True, False), (1 << 18, None, False)):
            with self.subTest(flags=flags, public=public), patch.object(runner, 'get', return_value={'id': '9', 'flags': flags, 'bot_public': public}):
                if succeeds:
                    self.assertIn('private bot', runner.check_intent())
                else:
                    with self.assertRaises(smoke.CheckFailed):
                        runner.check_intent()

    def test_command_list_exact_including_duplicates(self):
        runner = smoke.Smoke()
        runner.check_config()
        runner.application = {'id': '9'}
        for names in (['agents'], ['agents', 'send', 'extra'], ['agents', 'agents'], ['agents', 'send', 'send']):
            with self.subTest(names=names), patch.object(runner, 'get', return_value=[{'name': name} for name in names]):
                with self.assertRaises(smoke.CheckFailed):
                    runner.check_commands()

    def test_mcp_expansion_and_actual_allowlist(self):
        import mcp.client.stdio as stdio
        original = stdio.DEFAULT_INHERITED_ENV_VARS
        env = {'PATH': 'path', 'USERPROFILE': 'profile', 'PATHEXT': 'excluded',
               'PYTHONPATH': 'excluded', 'AI_AGENTS_HOME': 'excluded', 'DISCORD_BOT_TOKEN': TOKEN}
        with patch.dict(os.environ, env, clear=True):
            asyncio.run(smoke.check_mcp('claude'))
            asyncio.run(smoke.check_mcp('codex'))
        claude, _ = self.mcp_runs[0]
        codex, defaults = self.mcp_runs[1]
        self.assertEqual(claude.args[0], smoke.REPO.as_posix() + '/ai_agents_mcp.py')
        self.assertNotIn('${CLAUDE_PLUGIN_ROOT}', claude.args[0])
        self.assertEqual(codex.args, ['./ai_agents_mcp.py', '--host', 'codex'])
        self.assertEqual(codex.cwd, str(smoke.REPO.resolve()))
        self.assertEqual(codex.env, {'PATH': 'path', 'USERPROFILE': 'profile', 'DISCORD_BOT_TOKEN': TOKEN})
        self.assertEqual(defaults, {})
        self.assertIs(stdio.DEFAULT_INHERITED_ENV_VARS, original)

    def test_sdk_process_receives_only_allowlisted_environment(self):
        import mcp.client.stdio as stdio
        # Exercise the real SDK's env merge up to its mocked process launch.
        captured = []
        async def launch(**kwargs):
            captured.append(kwargs)
            raise OSError(TOKEN)
        with patch.object(stdio, 'stdio_client', sdk_stdio_client), \
                patch.dict(os.environ, {'PATH': 'path', 'USERPROFILE': 'profile', 'PATHEXT': 'excluded'}, clear=True), \
                patch.object(stdio, '_get_executable_command', side_effect=lambda command: command), \
                patch.object(stdio, '_create_platform_compatible_process', side_effect=launch):
            with self.assertRaises(OSError):
                asyncio.run(smoke.check_mcp('codex'))
        self.assertEqual(captured[0]['env'], {'PATH': 'path', 'USERPROFILE': 'profile'})
        self.assertEqual(captured[0]['cwd'], str(smoke.REPO.resolve()))

    def test_mcp_tool_set_exact_and_sdk_setting_restored_on_failure(self):
        import mcp.client.stdio as stdio
        original = stdio.DEFAULT_INHERITED_ENV_VARS
        for names in (list(smoke.TOOLS)[:-1], list(smoke.TOOLS) + ['unexpected'], list(smoke.TOOLS) + [next(iter(smoke.TOOLS))]):
            self.session.list_tools.return_value = SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])
            with self.assertRaises(smoke.CheckFailed):
                asyncio.run(smoke.check_mcp('codex'))
            self.assertIs(stdio.DEFAULT_INHERITED_ENV_VARS, original)

    def test_hooks_exact_commands_all_events_and_shells(self):
        smoke.check_hooks()
        expected = []
        for host in ('claude', 'codex'):
            for event, command, timeout in smoke.hook_commands(host):
                for shell in (('bash',) if host == 'claude' else ('pwsh', 'cmd')):
                    expanded = command.replace('${CLAUDE_PLUGIN_ROOT}', smoke.REPO.as_posix())
                    argv = ([shell, '-NoProfile', '-Command', expanded] if shell == 'pwsh' else
                            [shell, '-c', expanded] if shell == 'bash' else [shell, '/c', expanded])
                    if shell == 'cmd' and os.name == 'nt':
                        argv = 'cmd /c ' + expanded
                    expected.append((argv, event, timeout))
        actual = [(argv, payload['hook_event_name'], kwargs['timeout']) for argv, kwargs, _, payload in self.hook_runs]
        self.assertEqual(actual, expected)
        for _, kwargs, root, _ in self.hook_runs:
            self.assertEqual(kwargs['env']['PLUGIN_ROOT'], str(smoke.REPO))
            self.assertFalse(root.exists())

    def test_wsl_bash_launcher_is_rejected(self):
        wsl = r'C:\Windows\System32\bash.exe'
        with patch.object(smoke.shutil, 'which', side_effect=lambda name: wsl if name == 'bash' else name):
            with self.assertRaises(smoke.CheckFailed) as caught:
                smoke.check_hooks()
        self.assertIn('WSL launcher', str(caught.exception))

    def test_claude_without_bash_uses_cmd(self):
        with patch.object(smoke.shutil, 'which', side_effect=lambda name: None if name == 'bash' else name):
            smoke.check_hooks()
        for argv, _, _, _ in self.hook_runs[:6]:
            if os.name == 'nt':
                self.assertTrue(argv.startswith('cmd /c py "'))
            else:
                self.assertEqual(argv[:2], ['cmd', '/c'])

    def test_hooks_cleanup_and_continue_after_process_failure(self):
        run = self.hook_process
        def fail(argv, **kwargs):
            result = run(argv, **kwargs)
            result.returncode = 1
            return result
        self.process.side_effect = fail
        code, lines = self.run_main(['--offline-only'])
        self.assertEqual(code, 1)
        self.assertIn('30/30 hook runs failed', lines[-1])
        self.assertEqual(len(self.hook_runs), 30)
        self.assertTrue(all(not root.exists() for _, _, root, _ in self.hook_runs))

    def test_hooks_cleanup_on_timeout_and_bad_registry(self):
        roots = []
        def timeout(argv, **kwargs):
            roots.append(Path(kwargs['env']['AI_AGENTS_HOME']))
            raise subprocess.TimeoutExpired(argv, 1, output=TOKEN)
        self.process.side_effect = timeout
        with self.assertRaises(subprocess.TimeoutExpired):
            smoke.check_hook('codex', 'SessionStart', 'exact command', 1, 'cmd')
        self.assertFalse(roots[0].exists())
        def wrong_cwd(argv, **kwargs):
            result = self.hook_process(argv, **kwargs)
            state = agent_state.State(kwargs['env']['AI_AGENTS_HOME'])
            try:
                state.db.execute("UPDATE host_sessions SET cwd='wrong'")
            finally:
                state.close()
            return result
        self.process.side_effect = wrong_cwd
        with self.assertRaisesRegex(smoke.CheckFailed, 'exact host, cwd and event'):
            smoke.check_hook('codex', 'SessionStart', 'exact --host codex', 1, 'cmd')
        self.assertFalse(self.hook_runs[-1][2].exists())


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(agent_state, 'ROOT', self.root))

    def test_log_marker_after_last_start_and_lock(self):
        log = self.root / 'daemon.log'
        for text, held, succeeds in [('Listener started\nGateway ready', True, True),
                                     ('Listener started\nGateway resumed', True, True),
                                     ('Listener started\nGateway ready\nListener started', True, False),
                                     ('Gateway ready', True, False),
                                     ('Listener started\nGateway ready', False, False)]:
            with self.subTest(text=text, held=held), patch.object(smoke, 'listener_lock_held', return_value=held):
                log.write_text(text + TOKEN + HOOK, encoding='utf-8')
                if succeeds:
                    self.assertIn('lock held: yes', smoke.check_listener())
                else:
                    with self.assertRaises(smoke.CheckFailed) as failure:
                        smoke.check_listener()
                    self.assertNotIn(TOKEN, str(failure.exception))
                    self.assertNotIn(HOOK, str(failure.exception))
                    if not held:
                        self.assertIn('py ai_agents_daemon.py run', str(failure.exception))

    def test_lock_probe_does_not_create_missing_file(self):
        path = self.root / 'daemon.lock'
        self.assertFalse(smoke.listener_lock_held(path))
        self.assertFalse(path.exists())

    def test_lock_held_unlocked_and_unexpected_errors(self):
        import errno
        import msvcrt
        path = self.root / 'daemon.lock'
        path.write_bytes(b'\0')
        with patch.object(msvcrt, 'locking') as lock:
            self.assertFalse(smoke.listener_lock_held(path))
            self.assertEqual([call.args[1:] for call in lock.call_args_list],
                             [(msvcrt.LK_NBLCK, 1), (msvcrt.LK_UNLCK, 1)])
        with patch.object(msvcrt, 'locking', side_effect=OSError(errno.EACCES, TOKEN)):
            self.assertTrue(smoke.listener_lock_held(path))
        with patch.object(msvcrt, 'locking', side_effect=OSError(errno.EIO, TOKEN)):
            with self.assertRaises(OSError):
                smoke.listener_lock_held(path)


if __name__ == '__main__':
    unittest.main()
