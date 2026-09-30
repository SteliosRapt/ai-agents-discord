"""Local configuration and free SQLite session registry."""
import argparse
import ctypes
import os
from pathlib import Path
import re
import sqlite3
import time
import tomllib
import uuid

# Runtime data lives outside the plugin folder: Claude and Codex each install their own copy of the
# plugin, but config, token and the session registry must be one shared set.
ROOT = Path.home() / '.ai-agents'

def load_config(root=None):
    root = Path(root) if root is not None else ROOT
    path = root / 'config.toml'
    if not path.is_file():
        raise RuntimeError(f'Missing {path}; copy config.example.toml there and fill it in')
    with path.open('rb') as stream:
        config = tomllib.load(stream)
    required = ('guild_id', 'owner_user_id', 'agents_category_id', 'upload_roots')
    for key in required:
        if key not in config:
            raise ValueError('Missing config key: ' + key)
    for key in ('guild_id', 'owner_user_id', 'agents_category_id'):
        if not isinstance(config[key], str) or not re.fullmatch(r'[0-9]+', config[key]):
            raise ValueError(key + ' must be a string of digits')
    roots = config['upload_roots']
    if not isinstance(roots, list) or any(not isinstance(p, str) or not p for p in roots):
        raise ValueError('upload_roots must be a list of paths')
    config['upload_roots'] = [(root / p).resolve() for p in roots]
    ids = config.get('read_channel_ids', [])
    if not isinstance(ids, list) or any(not isinstance(i, str) or not re.fullmatch(r'[0-9]+', i) for i in ids):
        raise ValueError('read_channel_ids must be a list of strings of digits')
    config['read_channel_ids'] = ids
    return config

def process_alive(pid):
    """Query Windows process state without sending a signal."""
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = kernel.GetLastError()
        if error == 87:  # Invalid parameter: this PID no longer exists.
            return False
        raise RuntimeError(f'Cannot query process {pid}; Windows error {error}')
    try:
        code = ctypes.c_ulong()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise RuntimeError(f'Cannot read process state for {pid}')
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)

class State:
    def __init__(self, root=None):
        root = Path(root) if root is not None else ROOT
        self.db = sqlite3.connect(root / 'state.db', timeout=30, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, host TEXT, pid INTEGER, ppid INTEGER, cwd TEXT,
                project_dir TEXT, started_at REAL, last_seen REAL, status TEXT,
                title TEXT, plan TEXT, channel_id TEXT, thread_id TEXT,
                status_message_id TEXT
            );
            CREATE TABLE IF NOT EXISTS project_channels (
                project_dir TEXT PRIMARY KEY, channel_id TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, session_id TEXT, thread_id TEXT,
                created_at REAL, answered_message_id TEXT
            );
        ''')

    def register(self, host):
        session_id, now = uuid.uuid4().hex, time.time()
        self.db.execute('INSERT INTO sessions (id, host, pid, ppid, cwd, started_at, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        (session_id, host, os.getpid(), os.getppid(), str(Path.cwd()), now, now, 'connected'))
        return session_id

    def session(self, session_id):
        return self.db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()

    def update(self, session_id, **values):
        allowed = {'project_dir', 'status', 'title', 'plan', 'channel_id', 'thread_id', 'status_message_id'}
        if not values.keys() <= allowed:
            raise ValueError('Invalid session field')
        values['last_seen'] = time.time()
        self.db.execute('UPDATE sessions SET ' + ', '.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), session_id))

    def project_channel(self, project_dir, create):
        """Serialize mapping creation across processes."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT channel_id FROM project_channels WHERE project_dir=?', (str(project_dir),)).fetchone()
            if row:
                channel_id = row['channel_id']
            else:
                now = time.time()
                channel_id = create()['id']
                self.db.execute('INSERT INTO project_channels VALUES (?, ?, ?)', (str(project_dir), channel_id, now))
            self.db.execute('COMMIT')
            return channel_id
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['sessions'])
    parser.parse_args()
    state = State()
    try:
        for row in state.db.execute("SELECT * FROM sessions WHERE status NOT IN ('closed', 'ended') ORDER BY started_at"):
            if process_alive(row['pid']):
                folder = row['project_dir'] or row['cwd']
                age = int(time.time() - row['started_at'])
                print(f'{row["host"]} pid={row["pid"]} project={folder} title={row["title"] or ""} thread={row["thread_id"] or ""} age={age}s')
    finally:
        state.close()

if __name__ == '__main__':
    main()
