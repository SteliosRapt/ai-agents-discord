"""Local configuration and free SQLite session registry."""
import argparse
import ctypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import tomllib
import uuid

# Runtime data lives outside the plugin folder: Claude and Codex each install their own copy of the
# plugin, but config, token and the session registry must be one shared set.
ROOT = Path(os.environ['AI_AGENTS_HOME']) if os.environ.get('AI_AGENTS_HOME') else Path.home() / '.ai-agents'
SCHEMA_VERSION = 1
SHELLS = {'py.exe', 'pyw.exe', 'python.exe', 'pythonw.exe', 'cmd.exe', 'bash.exe',
          'sh.exe', 'pwsh.exe', 'powershell.exe', 'conhost.exe', 'git-bash.exe'}

class _ProcessEntry(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_ulong), ('cntUsage', ctypes.c_ulong),
                ('th32ProcessID', ctypes.c_ulong), ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', ctypes.c_ulong), ('cntThreads', ctypes.c_ulong),
                ('th32ParentProcessID', ctypes.c_ulong), ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', ctypes.c_ulong), ('szExeFile', ctypes.c_wchar * 260)]

def host_process_pid(pid=None):
    """Find the first non-shell ancestor using a Windows process snapshot."""
    pid = os.getpid() if pid is None else pid
    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    for name in ('Process32FirstW', 'Process32NextW'):
        function = getattr(kernel, name)
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
        function.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise RuntimeError(f'Cannot snapshot processes; Windows error {kernel.GetLastError()}')
    try:
        processes, entry = {}, _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            processes[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        error = kernel.GetLastError()
        if error != 18:  # ERROR_NO_MORE_FILES is the normal end of a snapshot.
            raise RuntimeError(f'Cannot enumerate processes; Windows error {error}')
        visited = {pid}
        while pid in processes:
            pid = processes[pid][0]
            if not pid or pid not in processes:
                return None
            if pid in visited:
                raise RuntimeError('Cycle in Windows process parent chain')
            visited.add(pid)
            if processes[pid][1] not in SHELLS:
                return pid
        return None
    finally:
        if not kernel.CloseHandle(snapshot):
            raise RuntimeError(f'Cannot close process snapshot; Windows error {kernel.GetLastError()}')

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
    minutes = config.get('long_turn_minutes', 10)
    if type(minutes) is not int or minutes < 1:
        raise ValueError('long_turn_minutes must be an integer of at least 1')
    mentions = config.get('alert_mentions_owner', True)
    if type(mentions) is not bool:
        raise ValueError('alert_mentions_owner must be a boolean')
    config['long_turn_minutes'] = minutes
    config['alert_mentions_owner'] = mentions
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
        self._inbox_owner, self._inbox_claimed = uuid.uuid4().hex, False
        self._inbox_previous = {}
        self.db = sqlite3.connect(root / 'state.db', timeout=30, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=30000')
        try:
            if self.db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                self.db.execute('PRAGMA journal_mode=WAL')
            if self.db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                self._migrate()
        except BaseException:
            self.db.close()
            raise

    def _migrate(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError('State database uses a newer schema version')
            if version == SCHEMA_VERSION:
                self.db.execute('COMMIT')
                return
            schema = '''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, host TEXT, pid INTEGER, ppid INTEGER, cwd TEXT,
                project_dir TEXT, started_at REAL, last_seen REAL, status TEXT,
                title TEXT, plan TEXT, channel_id TEXT, thread_id TEXT,
                status_message_id TEXT
            );
            CREATE TABLE IF NOT EXISTS project_channels (
                project_dir TEXT PRIMARY KEY, channel_id TEXT, created_at REAL,
                status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('creating', 'ready')), reserved_at REAL
            );
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, session_id TEXT, thread_id TEXT,
                created_at REAL, answered_message_id TEXT
            );
            CREATE TABLE IF NOT EXISTS host_sessions (
                host_session_id TEXT PRIMARY KEY, host TEXT, handle TEXT UNIQUE, cwd TEXT,
                host_pid INTEGER, started_at REAL, last_seen REAL, last_event TEXT,
                ended_at REAL, mcp_session_id TEXT, thread_id TEXT, title TEXT,
                last_prompt_at REAL
            );
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, host_session_id TEXT, path TEXT,
                note TEXT, sender_id TEXT, created_at REAL, delivered_at REAL,
                confirmed_at REAL, delivered_event TEXT, delivery_pid INTEGER, delivery_owner TEXT
            );
            '''
            for statement in schema.split(';'):
                if statement.strip():
                    self.db.execute(statement)
            for table, column, kind in (('inbox', 'confirmed_at', 'REAL'), ('inbox', 'delivered_event', 'TEXT'),
                                        ('inbox', 'delivery_pid', 'INTEGER'), ('inbox', 'delivery_owner', 'TEXT'),
                                        ('host_sessions', 'last_prompt_at', 'REAL'),
                                        ('project_channels', 'status', "TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('creating', 'ready'))"),
                                        ('project_channels', 'reserved_at', 'REAL')):
                columns = {row['name'] for row in self.db.execute('PRAGMA table_info(' + table + ')')}
                if column not in columns:
                    self.db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' ' + kind)
            self.db.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

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

    def project_channel(self, project_dir, create, *, lookup=None):
        """Reserve briefly, then reconcile/create without holding a write lock.

        Failed requests leave a reservation so a later lookup can recover an
        uncertain Discord creation after the 120-second reservation expires.
        """
        project_dir, now = str(project_dir), time.time()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM project_channels WHERE project_dir=?', (project_dir,)).fetchone()
            if row and row['status'] == 'ready':
                self.db.execute('COMMIT')
                return row['channel_id']
            if row and row['reserved_at'] is not None and now - row['reserved_at'] < 120:
                raise RuntimeError('project channel creation in progress, retry shortly')
            self.db.execute("INSERT INTO project_channels (project_dir, created_at, status, reserved_at) VALUES (?, ?, 'creating', ?) "
                            "ON CONFLICT(project_dir) DO UPDATE SET status='creating', reserved_at=excluded.reserved_at",
                            (project_dir, now, now))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        channel = lookup() if lookup is not None else None
        channel_id = (channel if channel is not None else create())['id']
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed = self.db.execute("UPDATE project_channels SET channel_id=?, status='ready' "
                                      "WHERE project_dir=? AND status='creating' AND reserved_at=?",
                                      (channel_id, project_dir, now)).rowcount
            if not changed:
                raise RuntimeError('Project channel reservation expired or was replaced; retry shortly')
            self.db.execute('COMMIT')
            return channel_id
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self, *, inbox_delivered=True):
        try:
            self.finish_inbox(delivered=inbox_delivered)
        finally:
            self.db.close()

    def touch_host_session(self, host, host_session_id, cwd, host_pid, event):
        now = time.time()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM host_sessions WHERE host_session_id=?', (host_session_id,)).fetchone()
            if not row:
                while True:
                    handle = ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(4))
                    if not self.host_session_by_handle(handle):
                        break
                self.db.execute('INSERT INTO host_sessions (host_session_id, host, handle, started_at) VALUES (?, ?, ?, ?)',
                                (host_session_id, host, handle, now))
            self.db.execute('UPDATE host_sessions SET last_seen=?, last_event=?, cwd=?, host_pid=?, ended_at=? WHERE host_session_id=?',
                            (now, event, str(cwd), host_pid, now if event == 'SessionEnd' else None, host_session_id))
            if event == 'UserPromptSubmit':
                self.db.execute('UPDATE host_sessions SET last_prompt_at=? WHERE host_session_id=?', (now, host_session_id))
            row = self.db.execute('SELECT * FROM host_sessions WHERE host_session_id=?', (host_session_id,)).fetchone()
            self.db.execute('COMMIT')
            return row
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def link_host_session(self, host_session_id, mcp_session_id, thread_id, title):
        self.db.execute('UPDATE host_sessions SET mcp_session_id=?, thread_id=?, title=? WHERE host_session_id=?',
                        (mcp_session_id, thread_id, title, host_session_id))

    def live_host_sessions(self, max_idle_seconds=43200):
        rows = self.db.execute('SELECT * FROM host_sessions WHERE ended_at IS NULL AND last_seen>=? ORDER BY last_seen DESC',
                               (time.time() - max_idle_seconds,)).fetchall()
        return [row for row in rows if row['host_pid'] is None or process_alive(row['host_pid'])]

    def host_session_by_handle(self, handle):
        return self.db.execute('SELECT * FROM host_sessions WHERE handle=?', (handle,)).fetchone()

    def add_inbox(self, host_session_id, path, note, sender_id):
        return self.db.execute('INSERT INTO inbox (host_session_id, path, note, sender_id, created_at) VALUES (?, ?, ?, ?, ?)',
                               (host_session_id, str(path), note, sender_id, time.time())).lastrowid

    def confirm_inbox(self, host_session_id, event, before):
        """A tool step proves progress after tool delivery; Stop also confirms prompt delivery."""
        if event not in ('PostToolUse', 'Stop'):
            return
        sql = 'SELECT * FROM inbox WHERE host_session_id=? AND confirmed_at IS NULL AND delivered_at<? AND delivery_pid IS NULL'
        if event == 'PostToolUse':
            # A failed prompt may never reach the model; replay it at the first successful tool step.
            sql += " AND COALESCE(delivered_event, 'UserPromptSubmit') <> 'UserPromptSubmit'"
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows = self.db.execute(sql, (host_session_id, before)).fetchall()
            self.db.executemany('UPDATE inbox SET confirmed_at=?, delivery_pid=NULL, delivery_owner=NULL WHERE id=?',
                                ((before, row['id']) for row in rows))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _inactive_inbox(self, rows):
        alive = {}
        available = []
        for row in rows:
            pid = row['delivery_pid']
            if pid is not None:
                if pid not in alive:
                    alive[pid] = process_alive(pid)
                if alive[pid]:
                    continue
            available.append(row)
        return available

    def finish_inbox(self, *, delivered=True):
        """Release this State's claims after output/cleanup; dead processes are recoverable on take."""
        if self._inbox_claimed:
            # An event that began while output was active must not replay it after release either.
            if delivered:
                self.db.execute('UPDATE inbox SET delivered_at=?, delivery_pid=NULL, delivery_owner=NULL WHERE delivery_owner=?',
                                (time.time(), self._inbox_owner))
            else:
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    self.db.executemany('UPDATE inbox SET delivered_at=?, delivered_event=?, delivery_pid=?, delivery_owner=? '
                                        'WHERE id=? AND delivery_owner=?',
                                        ((*previous, item, self._inbox_owner) for item, previous in self._inbox_previous.items()))
                    self.db.execute('COMMIT')
                except BaseException:
                    self.db.execute('ROLLBACK')
                    raise
            self._inbox_claimed = False
            self._inbox_previous.clear()

    def take_inbox(self, host_session_id, *, retry_before=None, event=None):
        """Claim pending items, optionally replaying unconfirmed deliveries older than this event."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows = self.db.execute('SELECT * FROM inbox WHERE host_session_id=? AND confirmed_at IS NULL '
                                   'AND (delivered_at IS NULL OR delivered_at<?) ORDER BY id',
                                   (host_session_id, retry_before)).fetchall()
            rows = self._inactive_inbox(rows)
            if rows:
                self._inbox_claimed = True
                self._inbox_previous.update((row['id'], (row['delivered_at'], row['delivered_event'], row['delivery_pid'], row['delivery_owner'])) for row in rows)
                now, pid = time.time(), os.getpid()
                self.db.executemany('UPDATE inbox SET delivered_at=?, delivered_event=?, delivery_pid=?, delivery_owner=? WHERE id=?',
                                    ((now, event, pid, self._inbox_owner, row['id']) for row in rows))
            self.db.execute('COMMIT')
            return rows
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['sessions', 'hosts'])
    args = parser.parse_args()
    state = State()
    try:
        if args.action == 'hosts':
            for row in state.live_host_sessions():
                folder = Path(row['cwd']).name
                idle = int(time.time() - row['last_seen'])
                print(f'{row["handle"]} {row["host"]} project={folder} title={row["title"] or ""} thread={row["thread_id"] or ""} idle={idle}s')
            return
        for row in state.db.execute("SELECT * FROM sessions WHERE status NOT IN ('closed', 'ended') ORDER BY started_at"):
            if process_alive(row['pid']):
                folder = row['project_dir'] or row['cwd']
                age = int(time.time() - row['started_at'])
                print(f'{row["host"]} pid={row["pid"]} project={folder} title={row["title"] or ""} thread={row["thread_id"] or ""} age={age}s')
    finally:
        state.close()

if __name__ == '__main__':
    main()
