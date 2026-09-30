"""Owner-only Discord listener and Windows login-task management."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import ctypes
from datetime import datetime, UTC
import errno
import msvcrt
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import agent_state
from bot_client import BotClient, bot_token, snowflake
from discord_gateway import Gateway, GatewayFatal

TASK = 'AI Agents Discord listener'
COMMANDS = [
    {'name': 'agents', 'type': 1, 'description': 'List running Claude Code / Codex sessions',
     'default_member_permissions': '0', 'contexts': [0]},
    {'name': 'send', 'type': 1, 'description': 'Send a file to one running agent session',
     'default_member_permissions': '0', 'contexts': [0], 'options': [
         {'type': 3, 'name': 'agent', 'description': 'Session handle (see /agents)', 'required': True, 'autocomplete': True},
         {'type': 11, 'name': 'file', 'description': 'File for the agent', 'required': True},
         {'type': 3, 'name': 'note', 'description': 'What the agent should do with it', 'required': False, 'max_length': 1000}]}
]

class Logger:
    def __init__(self, root=None):
        self.path = (agent_state.ROOT if root is None else Path(root)) / 'daemon.log'
        self.lock = threading.RLock()
        self.secrets = set()

    def add_secret(self, value):
        if isinstance(value, str) and value:
            with self.lock:
                self.secrets.add(value)
                self.secrets.add(urllib.parse.quote(value, safe=''))

    def redact(self, message):
        with self.lock:
            text = str(message)
            for secret in sorted(self.secrets, key=len, reverse=True):
                text = text.replace(secret, '[redacted]')
            text = re.sub(r'(?i)(?:https://(?:discord\.com|discordapp\.com)/api/(?:v[0-9]+/)?)?/(?:interactions|webhooks)/[^/\s]+/[^/\s"\']+', '[redacted]', text)
            return re.sub(r'(?i)authorization\b[^\r\n]*', '[redacted]', text)

    def __call__(self, message):
        with self.lock:
            text = self.redact(message).replace('\r', ' ').replace('\n', ' ')[:8000]
            line = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC ') + text + '\n'
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(line.encode('utf-8')) > 1024 * 1024:
                self.path.replace(self.path.with_name('daemon.log.1'))
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line)

class DaemonLock:
    def __enter__(self):
        agent_state.ROOT.mkdir(parents=True, exist_ok=True)
        self.stream = (agent_state.ROOT / 'daemon.lock').open('a+b')
        try:
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b'\0')
                self.stream.flush()
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.stream.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError('Listener already running') from None
            raise
        return self

    def __exit__(self, *args):
        try:
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.stream.close()

def safe_name(name):
    name = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[^A-Za-z0-9._ -]', '_', name).strip(' .') or 'file'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', name.split('.', 1)[0].rstrip(' ')):
        name = '_' + name
    suffix = Path(name).suffix
    if len(name) > 120:
        suffix = suffix[:119]
        name = name[:120 - len(suffix)] + suffix
    return name

def attachment_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('cdn.discordapp.com', 'media.discordapp.net') or
            parsed.username is not None or parsed.password is not None):
        raise ValueError('Attachment URL must use HTTPS on a Discord CDN host')
    return url

class AttachmentRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        attachment_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def urlopen(request, timeout):
    # Validate every redirect before issuing its request, not after leaving the CDN.
    return urllib.request.build_opener(AttachmentRedirect()).open(request, timeout=timeout)

def download(attachment, handle):
    url = attachment_url(attachment['url'])
    if not re.fullmatch(r'[a-z0-9]{4}', handle):
        raise ValueError('Invalid session handle')
    size = attachment.get('size')
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError('Invalid attachment size')
    root = agent_state.ROOT.resolve()
    inbox = (root / 'inbox').resolve()
    folder = (inbox / handle).resolve()
    if not inbox.is_relative_to(root) or not folder.is_relative_to(inbox):
        raise ValueError('Inbox folder escapes the data directory')
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now(UTC).strftime('%Y%m%d-%H%M%S_') + safe_name(attachment.get('filename', 'file'))
    index = 0
    while True:
        base = Path(name)
        candidate = name if index == 0 else f'{base.stem}_{index}{base.suffix}'
        path = (folder / candidate).resolve()
        part = folder / (candidate + '.part')
        if not path.is_relative_to(folder) or not part.resolve().is_relative_to(folder):
            raise ValueError('Attachment path escapes the inbox folder')
        if path.exists():
            index += 1
            continue
        try:
            stream = part.open('xb')
            break
        except FileExistsError:
            index += 1
    try:
        with stream, urlopen(urllib.request.Request(url), timeout=55) as response:
            count = 0
            while chunk := response.read(65536):
                stream.write(chunk)
                count += len(chunk)
            if size is not None and count != size:
                raise ValueError('Attachment size mismatch')
        part.rename(path)
        return path
    finally:
        part.unlink(missing_ok=True)

def project(row):
    return Path(row['cwd']).name

def agents_text(rows):
    if not rows:
        return 'No running Claude Code or Codex sessions with the plugin.'
    lines = []
    for index, row in enumerate(rows):
        title = ' '.join((row['title'] or 'no Discord task').splitlines())
        age = max(0, int((time.time() - row['last_seen']) / 60))
        line = f'`{row["handle"]}` {row["host"]} · {project(row)} · {title} · active {age}m ago'
        if row['thread_id']:
            line += f' · <#{row["thread_id"]}>'
        if len('\n'.join(lines + [line])) <= 2000:
            lines.append(line)
            continue
        remaining = len(rows) - index
        while True:
            marker = f'…and {remaining} more'
            if len('\n'.join(lines + [marker])) <= 2000:
                return '\n'.join(lines + [marker])
            lines.pop()
            remaining += 1
    return '\n'.join(lines)

class Listener:
    def __init__(self, config, client, app_id, logger):
        self.config, self.client, self.app_id, self.log = config, client, snowflake(app_id), logger
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='discord-interaction')

    def on_dispatch(self, name, data):
        if name != 'INTERACTION_CREATE':
            return
        self.log.add_secret(data.get('token'))
        self.pool.submit(self.handle, data).add_done_callback(self.finished)

    def finished(self, future):
        if not future.cancelled() and future.exception() is not None:
            error = future.exception()
            self.log(f'Interaction worker failed: {type(error).__name__}: {error}')

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)

    def callback(self, interaction, kind, data):
        data = dict(data, allowed_mentions={'parse': []})
        token = urllib.parse.quote(interaction['token'], safe='')
        self.client.request('POST', f'/interactions/{snowflake(interaction["id"])}/{token}/callback',
                            {'type': kind, 'data': data}, mentions=False)

    def edit(self, interaction, content):
        token = urllib.parse.quote(interaction['token'], safe='')
        self.client.request('PATCH', f'/webhooks/{self.app_id}/{token}/messages/@original',
                            {'content': self.log.redact(content)[:2000], 'allowed_mentions': {'parse': []}}, mentions=False)

    def handle(self, interaction):
        self.log.add_secret(interaction.get('token'))
        deferred = attempted = queued = False
        path = None

        def callback(kind, data):
            nonlocal attempted
            attempted = True
            self.callback(interaction, kind, data)

        try:
            kind = interaction.get('type')
            if kind not in (2, 4):
                return
            user = (interaction.get('member') or {}).get('user') or interaction.get('user') or {}
            if interaction.get('guild_id') != self.config['guild_id'] or user.get('id') != self.config['owner_user_id']:
                callback(8, {'choices': []}) if kind == 4 else callback(4, {'flags': 64, 'content': 'Only the owner can use these commands.'})
                return
            data = interaction['data']
            options = {option['name']: option.get('value') for option in data.get('options', [])}
            if kind == 4:
                focused = next((o for o in data.get('options', []) if o.get('focused') and o['name'] == 'agent'), None)
                choices = []
                if data.get('name') == 'send' and focused is not None:
                    with contextlib.closing(agent_state.State()) as state:
                        for row in state.live_host_sessions():
                            name = f'{row["handle"]} · {row["host"]} · {project(row)} · {row["title"] or "idle"}'[:100]
                            if str(focused.get('value', '')).casefold() in name.casefold():
                                choices.append({'name': name, 'value': row['handle']})
                                if len(choices) == 25:
                                    break
                callback(8, {'choices': choices})
            elif data['name'] == 'agents':
                with contextlib.closing(agent_state.State()) as state:
                    content = agents_text(state.live_host_sessions())
                callback(4, {'flags': 64, 'content': self.log.redact(content)})
            elif data['name'] == 'send':
                deferred = True
                callback(5, {'flags': 64})
                handle = options.get('agent', '')
                with contextlib.closing(agent_state.State()) as state:
                    row = next((r for r in state.live_host_sessions() if r['handle'] == handle), None)
                    if row is None:
                        self.edit(interaction, f'Unknown or inactive session `{handle}`. Use /agents.')
                        return
                    note = options.get('note', '')
                    if not isinstance(note, str) or len(note) > 1000:
                        raise ValueError('Note must be text of at most 1000 characters')
                    attachment = data['resolved']['attachments'][options['file']]
                    path = download(attachment, handle)
                    state.add_inbox(row['host_session_id'], path, note, self.config['owner_user_id'])
                    queued = True
                    if row['thread_id']:
                        content = f'📎 Owner sent `{path.name}` via /send' + (': ' + note if note else '')
                        self.client.send_message(row['thread_id'], self.log.redact(content)[:2000])
                    self.edit(interaction, f'Saved `{path.name}` for `{handle}` ({row["host"]} · {project(row)}). The agent sees it at its next step.')
            else:
                raise ValueError('Unknown command')
        except Exception as exc:
            message = self.log.redact(f'Failed: {type(exc).__name__}: {exc}')
            self.log(message)
            if path is not None and not queued:
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    self.log(f'Cannot remove failed download: {type(error).__name__}: {error}')
            try:
                if deferred:
                    self.edit(interaction, message)
                elif not attempted:
                    callback(8, {'choices': []}) if kind == 4 else callback(4, {'flags': 64, 'content': message[:2000]})
            except Exception as error:
                self.log(f'Cannot report interaction failure: {type(error).__name__}: {error}')

@contextlib.contextmanager
def shutdown_signals(stop_event):
    previous = {}
    for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        if hasattr(signal, name):
            number = getattr(signal, name)
            previous[number] = signal.signal(number, lambda *args: stop_event.set())
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)

    def handle_control(code):
        if code in (0, 1, 2, 5, 6):
            stop_event.set()
            return 1
        return 0

    handler = handler_type(handle_control)
    kernel = ctypes.windll.kernel32
    window = kernel.GetConsoleWindow
    window.argtypes = []
    window.restype = ctypes.c_void_p
    function = kernel.SetConsoleCtrlHandler
    function.argtypes = [handler_type, ctypes.c_int]
    function.restype = ctypes.c_int
    try:
        # pythonw.exe has no attached console and receives no console close events.
        if not window():
            yield
        else:
            if not function(handler, 1):
                raise RuntimeError('Cannot register Windows console close handler')
            try:
                yield
            finally:
                if not function(handler, 0):
                    raise RuntimeError('Cannot unregister Windows console close handler')
    finally:
        for number, old in previous.items():
            signal.signal(number, old)

def run_listener():
    with DaemonLock():
        logger = Logger()
        listener = None
        stop = threading.Event()
        result = 0
        try:
            token = bot_token()
            logger.add_secret(token)
            config = agent_state.load_config()
            client = BotClient(config['guild_id'])
            with shutdown_signals(stop):
                app_id = snowflake(client.request('GET', '/applications/@me')['id'])
                client.request('PUT', f'/applications/{app_id}/guilds/{config["guild_id"]}/commands', COMMANDS, mentions=False)
                url = client.request('GET', '/gateway/bot')['url']
                listener = Listener(config, client, app_id, logger)
                logger('Listener started')
                Gateway(token, listener.on_dispatch, url, intents=0, log=logger).run(stop)
            logger('Listener stopped')
        except KeyboardInterrupt:
            stop.set()
            logger('Listener stopped')
        except Exception as exc:
            logger(f'{type(exc).__name__}: {exc}')
            result = 1
        finally:
            stop.set()
            if listener is not None:
                try:
                    listener.close()
                except Exception as exc:
                    logger(f'Listener shutdown failed: {type(exc).__name__}: {exc}')
                    result = 1
        return result

def task_command(arguments):
    result = subprocess.run(['schtasks', *arguments], capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return result

def status():
    running = False
    if (agent_state.ROOT / 'daemon.lock').exists():
        try:
            with DaemonLock():
                pass
        except RuntimeError as exc:
            if str(exc) != 'Listener already running':
                raise
            running = True
    print('Listener running' if running else 'Listener not running')
    result = task_command(['/Query', '/TN', TASK])
    logger = Logger()
    print(f'Task query exit code: {result.returncode}')
    for text in (result.stdout, result.stderr):
        if text:
            print(logger.redact(text.strip()))
    if logger.path.exists():
        for line in logger.path.read_text(encoding='utf-8').splitlines()[-5:]:
            print(logger.redact(line))
    return 0

def install_autostart():
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError('pythonw.exe is missing next to the Python executable')
    command = f'"{pythonw}" "{Path(__file__).resolve()}" run'
    result = task_command(['/Create', '/TN', TASK, '/SC', 'ONLOGON', '/RL', 'LIMITED', '/F', '/TR', command])
    if result.returncode:
        raise RuntimeError(Logger().redact(f'Task creation failed: {result.stderr or result.stdout}'))
    print(f'Created {TASK}: {command}')
    return 0

def uninstall_autostart():
    result = task_command(['/Delete', '/TN', TASK, '/F'])
    if result.returncode:
        raise RuntimeError(Logger().redact(f'Task deletion failed: {result.stderr or result.stdout}'))
    print(f'Deleted {TASK}')
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'status', 'install-autostart', 'uninstall-autostart'])
    args = parser.parse_args(argv)
    try:
        return {'run': run_listener, 'status': status, 'install-autostart': install_autostart,
                'uninstall-autostart': uninstall_autostart}[args.action]()
    except Exception as exc:
        print(Logger().redact(f'{type(exc).__name__}: {exc}'), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
