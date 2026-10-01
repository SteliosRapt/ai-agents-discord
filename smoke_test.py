"""Read-only live entry-point checks; --offline-only checks local entry points."""
import argparse
import asyncio
from contextlib import contextmanager, redirect_stderr
import errno
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

import agent_state
import bot_client

REPO = Path(__file__).resolve().parent
TOOLS = {'start_session', 'post_milestone', 'update_status', 'send_video', 'ask_owner',
         'check_answer', 'read_messages', 'list_channels', 'end_session'}


class CheckFailed(Exception):
    """Only fixed, credential-free diagnostics belong in this exception."""


def mcp_parameters(host, repo=REPO):
    from mcp import StdioServerParameters
    spec = json.loads((repo / (host + '.mcp.json')).read_text(encoding='utf-8'))['mcpServers']['ai-agents']
    expand = lambda value: value.replace('${CLAUDE_PLUGIN_ROOT}', repo.as_posix())
    cwd = spec.get('cwd')
    if cwd is not None:
        cwd = str((repo / expand(cwd)).resolve())
    env = ({key: os.environ[key] for key in spec.get('env_vars', []) if key in os.environ}
           if host == 'codex' else dict(os.environ))
    return StdioServerParameters(command=expand(spec['command']),
                                 args=[expand(arg) for arg in spec.get('args', [])],
                                 cwd=cwd, env=env), spec.get('startup_timeout_sec', 20)


@contextmanager
def mcp_environment(host):
    # The SDK otherwise adds its own defaults even when an explicit env is supplied.
    # Checks run sequentially; restore the SDK setting before the next check.
    import mcp.client.stdio as stdio
    previous = stdio.DEFAULT_INHERITED_ENV_VARS
    try:
        if host == 'codex':
            stdio.DEFAULT_INHERITED_ENV_VARS = ()
        yield
    finally:
        stdio.DEFAULT_INHERITED_ENV_VARS = previous


async def check_mcp(host, repo=REPO):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    params, timeout = mcp_parameters(host, repo)
    # Child stderr and SDK diagnostics can contain arbitrary data; never echo them.
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as errors, redirect_stderr(errors), mcp_environment(host):
        async with asyncio.timeout(timeout):
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    names = [tool.name for tool in result.tools]
                    if len(names) != len(TOOLS) or set(names) != TOOLS:
                        raise CheckFailed('tool list differs from the nine required names')
    return 'initialized; exactly nine tools'


def hook_commands(host, repo=REPO):
    document = json.loads((repo / 'hooks' / (host + '-hooks.json')).read_text(encoding='utf-8'))
    for event, groups in document['hooks'].items():
        for group in groups:
            for hook in group['hooks']:
                if hook['type'] == 'command':
                    for key in ('command', 'command_windows'):
                        if key in hook:
                            yield event, hook[key], hook.get('timeout', 10)


def check_hook(host, event, command, timeout, shell, repo=REPO):
    # A new, unlinked session in an empty registry cannot trigger Discord alerts.
    with tempfile.TemporaryDirectory(prefix='ai-agents-smoke-') as temporary:
        root = Path(temporary)
        shutil.copyfile(agent_state.ROOT / 'config.toml', root / 'config.toml')
        env = dict(os.environ)
        env.update(AI_AGENTS_HOME=str(root), PLUGIN_ROOT=str(repo))
        session_id = 'smoke-' + uuid.uuid4().hex
        cwd = str(repo / 'Ελληνικά-😀')
        payload = json.dumps({'session_id': session_id, 'hook_event_name': event,
                              'cwd': cwd}, ensure_ascii=False).encode('utf-8')
        if host == 'claude':
            command = command.replace('${CLAUDE_PLUGIN_ROOT}', repo.as_posix())
        # Launch the PATH-resolved program: a bare 'bash' goes through CreateProcess's search order, which checks
        # System32 first and starts the WSL launcher instead of Git Bash (what Claude Code uses on Windows).
        program = shutil.which(shell) if shell != 'cmd' else shell
        if program is None:
            raise CheckFailed(f'{shell} not found on PATH')
        if shell == 'bash' and Path(program).parent.name.lower() == 'system32':
            raise CheckFailed('bash resolves to the WSL launcher; Claude Code uses Git Bash')
        argv = ([program, '-NoProfile', '-Command', command] if shell == 'pwsh' else
                [program, '-c', command] if shell == 'bash' else [shell, '/c', command])
        if shell == 'cmd' and os.name == 'nt':
            # list2cmdline escapes embedded quotes for the CRT, not cmd's parser.
            # Pass the exact shell program text without that extra quoting layer.
            argv = 'cmd /c ' + command
        result = subprocess.run(argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=repo, env=env, timeout=timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode != 0:
            raise CheckFailed('hook process returned a nonzero exit code')
        database = root / 'state.db'
        if not database.is_file():
            raise CheckFailed('hook did not create the isolated registry')
        connection = sqlite3.connect(database)
        try:
            row = connection.execute('SELECT host, cwd, last_event FROM host_sessions WHERE host_session_id=?',
                                     (session_id,)).fetchone()
        finally:
            connection.close()
        if row != (host, cwd, event):
            raise CheckFailed('hook did not record the exact host, cwd and event')


def check_hooks(repo=REPO):
    failures, count = [], 0
    for host in ('claude', 'codex'):
        shells = ('pwsh', 'cmd') if host == 'codex' else (('bash',) if shutil.which('bash') else ('cmd',))
        for event, command, timeout in hook_commands(host, repo):
            for shell in shells:
                count += 1
                try:
                    check_hook(host, event, command, timeout, shell, repo)
                except CheckFailed as exc:
                    failures.append(f'{host}/{event}/{shell}: {exc}')
                except Exception as exc:
                    failures.append(f'{host}/{event}/{shell}: {type(exc).__name__}')
    if not count:
        raise CheckFailed('no command hooks found')
    if failures:
        raise CheckFailed(f'{len(failures)}/{count} hook runs failed; ' + failures[0])
    return f'{count} shell runs recorded the exact UTF-8 cwd; temporary registries removed'


def listener_lock_held(path):
    import msvcrt
    if not path.is_file():
        return False
    with path.open('r+b') as stream:
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return True
            raise
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    return False


def check_listener():
    held = listener_lock_held(agent_state.ROOT / 'daemon.lock')
    path = agent_state.ROOT / 'daemon.log'
    log = path.read_text(encoding='utf-8') if path.is_file() else ''
    start = log.rfind('Listener started')
    ready = start >= 0 and any(marker in log[start:] for marker in ('Gateway ready', 'Gateway resumed'))
    reason = f'lock held: {"yes" if held else "no"}; Gateway ready/resumed after last start: {"yes" if ready else "no"}'
    if not held:
        raise CheckFailed(reason + '; py ai_agents_daemon.py run')
    if not ready:
        raise CheckFailed(reason + '; check daemon.log')
    return reason


def check_installed(repo=REPO, home=None):
    """Both apps run a cached copy of the plugin; it must be the version in this repo."""
    home = Path.home() if home is None else Path(home)
    problems, found = [], []
    claude_expected = json.loads((repo / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))['version']
    codex_expected = json.loads((repo / '.codex-plugin' / 'plugin.json').read_text(encoding='utf-8'))['version']
    record = home / '.claude' / 'plugins' / 'installed_plugins.json'
    entries = (json.loads(record.read_text(encoding='utf-8')).get('plugins', {}) if record.is_file() else {}).get('ai-agents@ai-agents-local', [])
    claude = entries[0].get('version') if entries else None
    found.append(f'claude {claude}')
    if claude != claude_expected:
        problems.append(f'Claude has {claude}, repo is {claude_expected} (claude plugin marketplace update ai-agents-local; claude plugin update ai-agents@ai-agents-local; restart)')
    cache = home / '.codex' / 'plugins' / 'cache' / 'ai-agents-local' / 'ai-agents'
    codex = sorted(d.name for d in cache.iterdir() if d.is_dir()) if cache.is_dir() else []
    found.append('codex ' + (','.join(codex) or 'none'))
    if codex != [codex_expected]:
        problems.append(f'Codex has {codex or "nothing"}, repo is {codex_expected} (codex plugin add ai-agents@ai-agents-local; restart)')
    if problems:
        raise CheckFailed('; '.join(problems))
    return 'installed: ' + ', '.join(found)


class Smoke:
    def __init__(self):
        self.config = None
        self.application = None

    def get(self, path):
        if self.config is None:
            raise CheckFailed('config check failed; fix config.toml')
        return bot_client.BotClient(self.config['guild_id']).request('GET', path)

    def check_config(self):
        self.config = agent_state.load_config()
        return 'config.toml loaded'

    def check_token(self):
        bot_client.bot_token()
        self.get('/users/@me')
        return 'credential loaded; GET /users/@me succeeded'

    def check_intent(self):
        self.application = self.get('/applications/@me')
        if not self.application.get('flags', 0) & ((1 << 18) | (1 << 19)):
            raise CheckFailed('Message Content intent is disabled')
        if self.application.get('bot_public') is not False:
            raise CheckFailed('bot_public must be false')
        return 'Message Content intent enabled; private bot'

    def check_access(self):
        if self.config is None:
            raise CheckFailed('config check failed; fix config.toml')
        self.get('/guilds/' + self.config['guild_id'])
        ids = dict.fromkeys([self.config['agents_category_id'], *self.config['read_channel_ids']])
        for channel_id in ids:
            self.get('/channels/' + channel_id)
        return 'guild, agents category and all read channels accessible'

    def check_commands(self):
        application = self.application if self.application is not None else self.get('/applications/@me')
        app_id = bot_client.snowflake(application['id'])
        commands = self.get(f'/applications/{app_id}/guilds/{self.config["guild_id"]}/commands')
        names = [command['name'] for command in commands]
        if len(names) != 2 or set(names) != {'agents', 'send'}:
            raise CheckFailed('guild commands must be exactly agents and send')
        return 'exactly agents and send registered'


def run_check(name, check):
    try:
        reason = check()
    except CheckFailed as exc:
        print(f'FAIL {name}: {exc}')
        return False
    except Exception as exc:
        # Exception messages, server responses and child output are never trusted diagnostics.
        print(f'FAIL {name}: {type(exc).__name__}; check local setup and permissions')
        return False
    print(f'PASS {name}: {reason}')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline-only', action='store_true')
    args = parser.parse_args(argv)
    smoke = Smoke()
    checks = [('config', smoke.check_config), ('installed', check_installed)]
    if not args.offline_only:
        checks += [('token', smoke.check_token), ('intent', smoke.check_intent),
                   ('access', smoke.check_access), ('commands', smoke.check_commands)]
    checks += [('mcp-claude', lambda: asyncio.run(check_mcp('claude'))),
               ('mcp-codex', lambda: asyncio.run(check_mcp('codex'))), ('hooks', check_hooks)]
    if not args.offline_only:
        checks.append(('listener', check_listener))
    results = [run_check(name, check) for name, check in checks]
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
