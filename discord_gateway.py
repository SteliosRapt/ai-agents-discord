"""Standard-library WebSocket transport and resumable Discord Gateway v10 loop."""
import base64
import hashlib
import json
import math
import os
import random
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

MAX_FRAME = 16 * 1024 * 1024
FATAL_CODES = {4004, 4010, 4011, 4012, 4013, 4014}

def print_to_stderr(message):
    print(message, file=sys.stderr)

class WebSocketClosed(Exception):
    def __init__(self, code, reason=''):
        self.code, self.reason = code, reason
        super().__init__(f'WebSocket closed ({code}): {reason}')

class GatewayFatal(RuntimeError):
    pass

class WebSocket:
    def __init__(self, sock, buffered=b''):
        self.sock = sock
        self.buffered = buffered
        self.send_lock = threading.Lock()
        self.closed = False

    @classmethod
    def connect(cls, url, timeout):
        if any(c in url for c in '\r\n'):
            raise ValueError('Invalid WebSocket URL')
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'wss' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('WebSocket requires a wss URL without credentials or fragment')
        host, port = parsed.hostname, parsed.port or 443
        raw = socket.create_connection((host, port), timeout=timeout)
        sock = raw
        try:
            # create_default_context enables certificate and hostname verification.
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            authority = '[' + host + ']' if ':' in host else host
            if port != 443:
                authority += ':' + str(port)
            request = (f'GET {path} HTTP/1.1\r\nHost: {authority}\r\n'
                       f'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                       'Sec-WebSocket-Version: 13\r\n\r\n')
            sock.sendall(request.encode('ascii'))
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError('WebSocket upgrade ended early')
                data += chunk
                if len(data.split(b'\r\n\r\n', 1)[0]) > 65536:
                    raise ValueError('WebSocket upgrade headers too large')
            head, buffered = data.split(b'\r\n\r\n', 1)
            lines = head.decode('iso-8859-1').split('\r\n')
            status = lines[0].split(' ', 2)
            if len(status) < 2 or status[:2] != ['HTTP/1.1', '101']:
                raise ValueError('WebSocket upgrade requires HTTP 101')
            headers = {}
            for line in lines[1:]:
                name, separator, value = line.partition(':')
                if not separator or name.lower() in headers:
                    raise ValueError('Invalid WebSocket upgrade headers')
                headers[name.lower()] = value.strip()
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
            if headers.get('sec-websocket-accept') != accept:
                raise ValueError('Invalid Sec-WebSocket-Accept')
            if (headers.get('upgrade', '').lower() != 'websocket' or
                    'upgrade' not in [v.strip().lower() for v in headers.get('connection', '').split(',')] or
                    'sec-websocket-extensions' in headers):
                raise ValueError('Invalid WebSocket upgrade negotiation')
            sock.settimeout(None)
            return cls(sock, buffered)
        except Exception:
            sock.close()
            if sock is not raw:
                raw.close()
            raise

    def _read(self, size):
        while len(self.buffered) < size:
            chunk = self.sock.recv(min(65536, size - len(self.buffered)))
            if not chunk:
                raise WebSocketClosed(1006, 'Connection ended without a close frame')
            self.buffered += chunk
        result, self.buffered = self.buffered[:size], self.buffered[size:]
        return result

    def _send(self, opcode, payload):
        length = len(payload)
        if length > MAX_FRAME:
            raise ValueError('WebSocket frame exceeds 16 MiB')
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += b'\xfe' + struct.pack('!H', length)
        else:
            header += b'\xff' + struct.pack('!Q', length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self.send_lock:
            if self.closed:
                raise WebSocketClosed(1006, 'Connection is closed')
            self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self._send(1, text.encode('utf-8'))

    def recv(self):
        message = bytearray()
        fragmented = False
        while True:
            first, second = self._read(2)
            final, opcode = bool(first & 0x80), first & 0x0f
            if first & 0x70 or second & 0x80:
                raise ValueError('Unsupported WebSocket flags or masked server frame')
            length = second & 0x7f
            if length == 126:
                length = struct.unpack('!H', self._read(2))[0]
                if length < 126:
                    raise ValueError('Noncanonical WebSocket length')
            elif length == 127:
                length = struct.unpack('!Q', self._read(8))[0]
                if length < 65536 or length & (1 << 63):
                    raise ValueError('Invalid WebSocket length')
            if length > MAX_FRAME:
                raise ValueError('WebSocket frame exceeds 16 MiB')
            if opcode >= 8 and (not final or length > 125):
                raise ValueError('Invalid WebSocket control frame')
            payload = self._read(length)
            if opcode == 8:
                if len(payload) == 1:
                    raise ValueError('Invalid WebSocket close payload')
                code = struct.unpack('!H', payload[:2])[0] if payload else 1005
                reason = payload[2:].decode('utf-8')
                try:
                    self._send(8, payload)
                except OSError:
                    pass
                finally:
                    self._shutdown()
                raise WebSocketClosed(code, reason)
            if opcode == 9:
                self._send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not fragmented:
                fragmented = True
            elif opcode != 0 or not fragmented:
                raise ValueError('Unexpected WebSocket message opcode')
            if len(message) + length > MAX_FRAME:
                raise ValueError('WebSocket message exceeds 16 MiB')
            message.extend(payload)
            if final:
                return message.decode('utf-8')

    def _shutdown(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def close(self, code=1000):
        if self.closed:
            return
        try:
            self._send(8, struct.pack('!H', code))
        except (OSError, WebSocketClosed):
            pass
        finally:
            self._shutdown()

class Gateway:
    def __init__(self, token, on_dispatch, gateway_url, *, intents=0, log=print_to_stderr):
        if not isinstance(token, str) or not token:
            raise ValueError('Missing bot credential')
        self.token, self.on_dispatch = token, on_dispatch
        self.gateway_url, self.intents, self.log = gateway_url, intents, log
        self.session_id = self.resume_gateway_url = self.last_seq = None

    def _safe(self, message):
        message = message.replace(self.token, '[redacted]')
        message = re.sub(r'(?i)https://(?:discord\.com|discordapp\.com)/api/(?:v[0-9]+/)?webhooks/[^\s"\']+', '[redacted]', message)
        return re.sub(r'(?i)authorization\b[^\r\n]*', '[redacted]', message)

    def _log_error(self, exc):
        try:
            self.log(self._safe(type(exc).__name__ + ': ' + str(exc)))
        except Exception as error:
            raise RuntimeError(self._safe('Gateway logger failed: ' + type(error).__name__ + ': ' + str(error))) from None

    def _connection(self, stop_event, ready):
        resumable = self.session_id is not None and self.last_seq is not None
        url = self.resume_gateway_url if resumable and self.resume_gateway_url else self.gateway_url
        parts = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit(parts._replace(query='v=10&encoding=json'))
        ws = WebSocket.connect(url, timeout=10)
        done = threading.Event()
        lock = threading.Lock()
        state = {'deadline': None, 'awaiting_ack': False, 'error': None}

        def send_heartbeat(check_ack=False):
            with lock:
                if check_ack and state['awaiting_ack']:
                    raise ConnectionError('Gateway heartbeat ACK missing; reconnecting')
                ws.send_text(json.dumps({'op': 1, 'd': self.last_seq}))
                state['awaiting_ack'] = True

        def heartbeat_loop():
            while not done.wait(0.05):
                if stop_event.is_set():
                    ws.close(1000)
                    return
                with lock:
                    deadline = state['deadline']
                if deadline is not None and time.monotonic() >= deadline:
                    try:
                        send_heartbeat(True)
                        with lock:
                            state['deadline'] = time.monotonic() + interval
                    except Exception as exc:
                        state['error'] = exc
                        ws.close(4000)
                        return

        thread = threading.Thread(target=heartbeat_loop, name='discord-heartbeat', daemon=True)
        thread.start()
        try:
            hello = json.loads(ws.recv())
            if hello.get('op') != 10:
                raise ValueError('Gateway expected HELLO')
            interval = hello['d']['heartbeat_interval'] / 1000
            if not math.isfinite(interval) or interval <= 0:
                raise ValueError('Invalid Gateway heartbeat interval')
            if stop_event.is_set():
                return 'stop'
            if resumable:
                payload = {'op': 6, 'd': {'token': self.token, 'session_id': self.session_id, 'seq': self.last_seq}}
            else:
                payload = {'op': 2, 'd': {'token': self.token, 'intents': self.intents,
                           'properties': {'os': 'windows', 'browser': 'ai-agents', 'device': 'ai-agents'}}}
            ws.send_text(json.dumps(payload))
            with lock:
                state['deadline'] = time.monotonic() + interval * random.random()
            while not stop_event.is_set():
                try:
                    event = json.loads(ws.recv())
                except Exception:
                    if state['error'] is not None:
                        raise state['error'] from None
                    raise
                op, data = event['op'], event.get('d')
                if op == 0:
                    self.last_seq = event.get('s')
                    name = event.get('t')
                    if name == 'READY':
                        self.session_id, self.resume_gateway_url = data['session_id'], data['resume_gateway_url']
                    if name in ('READY', 'RESUMED'):
                        ready()
                    try:
                        self.on_dispatch(name, data)
                    except Exception as exc:
                        self._log_error(exc)
                elif op == 11:
                    with lock:
                        state['awaiting_ack'] = False
                elif op == 1:
                    send_heartbeat()
                elif op == 7:
                    return 'resume'
                elif op == 9:
                    if data is not True:
                        self.session_id = self.resume_gateway_url = self.last_seq = None
                        return 'identify'
                    return 'resume'
            return 'stop'
        finally:
            done.set()
            # Discord invalidates sessions on 1000/1001; reconnects must keep them resumable.
            ws.close(1000 if stop_event.is_set() else 4000)
            thread.join()

    def run(self, stop_event):
        backoff = 1

        def ready():
            nonlocal backoff
            backoff = 1

        while not stop_event.is_set():
            try:
                action = self._connection(stop_event, ready)
                if action == 'stop':
                    return
                if action == 'identify':
                    stop_event.wait(random.uniform(1, 5))
            except WebSocketClosed as exc:
                if stop_event.is_set():
                    return
                if exc.code in FATAL_CODES:
                    raise GatewayFatal(f'Discord Gateway fatal close ({exc.code})') from None
                # Invalid sequence/session timeout cannot be resumed.
                if exc.code in (4007, 4009):
                    self.session_id = self.resume_gateway_url = self.last_seq = None
                self._log_error(exc)
                if stop_event.wait(backoff):
                    return
                backoff = min(60, backoff * 2)
            except Exception as exc:
                if stop_event.is_set():
                    return
                self._log_error(exc)
                if stop_event.wait(backoff):
                    return
                backoff = min(60, backoff * 2)
