"""Offline Gateway tests: fake TLS sockets and scripted WebSocket connections."""
import base64
import hashlib
import json
import ssl
import struct
import threading
import unittest
from unittest.mock import Mock, patch
import discord_gateway as gateway

TOKEN = 'offline-private-gateway-token'

def frame(payload=b'', opcode=1, final=True, masked=False):
    first = (0x80 if final else 0) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, length | (0x80 if masked else 0)])
    elif length < 65536:
        header = bytes([first, 126 | (0x80 if masked else 0)]) + struct.pack('!H', length)
    else:
        header = bytes([first, 127 | (0x80 if masked else 0)]) + struct.pack('!Q', length)
    if masked:
        mask = b'abcd'
        return header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
    return header + payload

def unmask(data):
    opcode, size = data[0] & 15, data[1] & 127
    offset = 2
    if size == 126:
        size, offset = struct.unpack('!H', data[2:4])[0], 4
    elif size == 127:
        size, offset = struct.unpack('!Q', data[2:10])[0], 10
    mask = data[offset:offset + 4]
    payload = data[offset + 4:]
    assert len(payload) == size
    return opcode, bytes(v ^ mask[i % 4] for i, v in enumerate(payload))

class MemorySocket:
    def __init__(self, incoming=b'', chunk=4096):
        self.incoming, self.chunk = incoming, chunk
        self.sent = []
        self.closed = False
        self.timeout = None

    def recv(self, size):
        size = min(size, self.chunk)
        result, self.incoming = self.incoming[:size], self.incoming[size:]
        return result

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True

class BlockingSocket(MemorySocket):
    def __init__(self, incoming=b'', block_op=None):
        super().__init__(incoming)
        self.block_op = block_op
        self.blocked = threading.Event()
        self.shutdown_called = threading.Event()

    def recv(self, size):
        if self.incoming:
            return super().recv(size)
        if not self.shutdown_called.wait(3):
            raise TimeoutError('Blocked receive was not interrupted')
        return b''

    def sendall(self, data):
        opcode, payload = unmask(data)
        op = json.loads(payload)['op'] if opcode == 1 and self.block_op is not None else 'close'
        self.sent.append(data)
        if self.block_op is None or op == self.block_op:
            self.blocked.set()
            # Intentionally ignore socket timeouts: only shutdown releases the write.
            if not self.shutdown_called.wait(3):
                raise TimeoutError('Blocked write was not interrupted')
            raise OSError('Write interrupted by shutdown')

    def shutdown(self, how):
        self.shutdown_called.set()

class WebSocketTests(unittest.TestCase):
    def test_send_mask_and_all_length_encodings(self):
        for length, marker in ((0, 0), (125, 125), (126, 126), (65535, 126), (65536, 127)):
            with self.subTest(length=length):
                sock = MemorySocket()
                ws = gateway.WebSocket(sock)
                with patch.object(gateway.os, 'urandom', return_value=b'abcd') as random_bytes:
                    ws.send_text('x' * length)
                data = sock.sent[0]
                self.assertEqual(data[0], 0x81)
                self.assertEqual(data[1], 0x80 | marker)
                self.assertEqual(unmask(data), (1, b'x' * length))
                random_bytes.assert_called_once_with(4)

    def test_receive_lengths_and_fragmented_unicode_with_control_frames(self):
        for length in (0, 125, 126, 65535, 65536):
            with self.subTest(length=length):
                self.assertEqual(gateway.WebSocket(MemorySocket(frame(b'x' * length), chunk=17)).recv(), 'x' * length)
        incoming = (frame(b'hello \xe2', final=False) + frame(b'ping', opcode=9) +
                    frame(b'ignored', opcode=10) + frame(b'\x82', opcode=0, final=False) +
                    frame(b'\xac', opcode=0) + frame(b'next'))
        sock = MemorySocket(incoming, chunk=1)
        ws = gateway.WebSocket(sock)
        self.assertEqual(ws.recv(), 'hello €')
        self.assertEqual(unmask(sock.sent[0]), (10, b'ping'))
        self.assertEqual(ws.recv(), 'next')

    def test_close_is_echoed_and_local_close_is_masked(self):
        for payload, code, reason in ((struct.pack('!H', 4004) + b'bad auth', 4004, 'bad auth'), (b'', 1005, '')):
            sock = MemorySocket(frame(payload, opcode=8))
            with self.assertRaises(gateway.WebSocketClosed) as raised:
                gateway.WebSocket(sock).recv()
            self.assertEqual((raised.exception.code, raised.exception.reason), (code, reason))
            self.assertEqual(unmask(sock.sent[0]), (8, payload))
            self.assertTrue(sock.closed)
        sock = MemorySocket()
        ws = gateway.WebSocket(sock)
        ws.close()
        ws.close()
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(unmask(sock.sent[0]), (8, struct.pack('!H', 1000)))

    def test_reject_invalid_and_oversized_frames_before_reading_payload(self):
        bad = [frame(b'secret', masked=True), b'\x81\x7f' + struct.pack('!Q', gateway.MAX_FRAME + 1),
               frame(b'x', opcode=0), frame(b'x', opcode=2), frame(b'x', opcode=9, final=False),
               frame(b'x' * 126, opcode=9), frame(b'x', opcode=8), b'\xc1\x00',
               b'\x81\x7e\x00\x01', b'\x81\x7f' + struct.pack('!Q', 1 << 63),
               frame(b'x', final=False) + frame(b'y')]
        for data in bad:
            with self.subTest(data=data[:10]):
                with self.assertRaises(ValueError):
                    gateway.WebSocket(MemorySocket(data)).recv()
        with patch.object(gateway, 'MAX_FRAME', 3):
            with self.assertRaisesRegex(ValueError, 'message exceeds'):
                gateway.WebSocket(MemorySocket(frame(b'ab', final=False) + frame(b'cd', opcode=0))).recv()

    def handshake(self, accept=None, status='101 Switching Protocols', extras=b''):
        key = base64.b64encode(b'k' * 16).decode('ascii')
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        response = (b'HTTP/1.1 ' + status.encode() + b'\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n'
                    b'Sec-WebSocket-Accept: ' + (expected if accept is None else accept) + b'\r\n' + extras + b'\r\n' + frame(b'buffered'))
        sock = MemorySocket(response)
        context = ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        wrapper = self.enterContext(patch.object(context, 'wrap_socket', return_value=sock))
        factory = self.enterContext(patch.object(gateway.ssl, 'create_default_context', return_value=context))
        connector = self.enterContext(patch.object(gateway.socket, 'create_connection', return_value=sock))
        self.enterContext(patch.object(gateway.os, 'urandom', return_value=b'k' * 16))
        return sock, wrapper, factory, connector

    def test_verified_tls_and_upgrade_retains_buffered_frame(self):
        sock, wrapper, factory, connector = self.handshake()
        ws = gateway.WebSocket.connect('wss://gateway.example:8443/path?v=10&encoding=json', 7)
        connector.assert_called_once_with(('gateway.example', 8443), timeout=7)
        factory.assert_called_once_with()
        wrapper.assert_called_once_with(sock, server_hostname='gateway.example')
        request = sock.sent[0].decode()
        self.assertTrue(request.startswith('GET /path?v=10&encoding=json HTTP/1.1\r\n'))
        self.assertIn('Host: gateway.example:8443\r\n', request)
        self.assertIn('Sec-WebSocket-Version: 13\r\n', request)
        self.assertIn('Sec-WebSocket-Key: ' + base64.b64encode(b'k' * 16).decode(), request)
        self.assertNotIn('Sec-WebSocket-Extensions', request)
        self.assertEqual(ws.recv(), 'buffered')

    def test_bad_accept_and_non_101_close_socket(self):
        for kwargs, message in (({'accept': b'wrong'}, 'Sec-WebSocket-Accept'),
                                ({'status': '200 OK'}, 'HTTP 101'),
                                ({'extras': b'Sec-WebSocket-Extensions: permessage-deflate\r\n'}, 'negotiation')):
            with self.subTest(kwargs=kwargs):
                sock, _, _, _ = self.handshake(**kwargs)
                with self.assertRaisesRegex(ValueError, message):
                    gateway.WebSocket.connect('wss://gateway.example', 1)
                self.assertTrue(sock.closed)

    def test_wss_only_and_tls_failure_closes_raw_socket(self):
        with patch.object(gateway.socket, 'create_connection') as connector:
            for url in ('ws://example', 'wss://user:password@example', 'wss://example/#fragment', 'wss://example/\r\nx'):
                with self.assertRaises(ValueError):
                    gateway.WebSocket.connect(url, 1)
            connector.assert_not_called()
        sock = MemorySocket()
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('untrusted certificate')
        with patch.object(gateway.socket, 'create_connection', return_value=sock), patch.object(gateway.ssl, 'create_default_context', return_value=context):
            with self.assertRaises(ssl.SSLCertVerificationError):
                gateway.WebSocket.connect('wss://example', 1)
        self.assertTrue(sock.closed)

    def test_close_interrupts_blocked_writer_without_waiting_for_write_timeout(self):
        sock = BlockingSocket()
        ws = gateway.WebSocket(sock)
        errors = []

        def write():
            try:
                ws.send_text('blocked')
            except Exception as exc:
                errors.append(exc)

        writer = threading.Thread(target=write, daemon=True)
        with patch.object(gateway, 'CLOSE_TIMEOUT', 0.05):
            writer.start()
            try:
                self.assertTrue(sock.blocked.wait(1))
                closer = threading.Thread(target=ws.close, daemon=True)
                closer.start()
                closer.join(1)
                self.assertFalse(closer.is_alive(), 'close waited for the blocked writer')
                self.assertTrue(sock.shutdown_called.is_set())
                writer.join(1)
                self.assertFalse(writer.is_alive(), 'shutdown did not release sendall')
                self.assertEqual(len(errors), 1)
            finally:
                sock.shutdown(gateway.socket.SHUT_RDWR)
                writer.join(1)

    def test_blocked_close_frame_has_bounded_write(self):
        sock = BlockingSocket()
        ws = gateway.WebSocket(sock)
        with patch.object(gateway, 'CLOSE_TIMEOUT', 0.05):
            closer = threading.Thread(target=ws.close, daemon=True)
            closer.start()
            try:
                self.assertTrue(sock.blocked.wait(1))
                closer.join(1)
                self.assertFalse(closer.is_alive(), 'close frame write stalled shutdown')
                self.assertTrue(sock.shutdown_called.is_set())
                self.assertEqual(unmask(sock.sent[0]), (8, struct.pack('!H', 1000)))
            finally:
                sock.shutdown(gateway.socket.SHUT_RDWR)
                closer.join(1)

    def test_blocked_data_write_times_out_and_shuts_down_socket(self):
        sock = BlockingSocket()
        ws = gateway.WebSocket(sock)
        with patch.object(gateway, 'WRITE_TIMEOUT', 0.05):
            with self.assertRaisesRegex(TimeoutError, 'WebSocket write timed out'):
                ws.send_text('blocked')
        self.assertTrue(sock.blocked.is_set())
        self.assertTrue(sock.shutdown_called.is_set())
        self.assertTrue(sock.closed)

    def test_remote_close_survives_blocked_echo_and_preserves_close_code(self):
        sock = BlockingSocket(frame(struct.pack('!H', 4004) + b'auth failed', opcode=8))
        ws = gateway.WebSocket(sock)
        errors = []

        def write():
            try:
                ws.send_text('blocked')
            except Exception as exc:
                errors.append(exc)

        writer = threading.Thread(target=write, daemon=True)
        with patch.object(gateway, 'CLOSE_TIMEOUT', 0.05):
            writer.start()
            try:
                self.assertTrue(sock.blocked.wait(1))
                with self.assertRaises(gateway.WebSocketClosed) as raised:
                    ws.recv()
                self.assertEqual((raised.exception.code, raised.exception.reason), (4004, 'auth failed'))
                self.assertTrue(sock.shutdown_called.is_set())
                writer.join(1)
                self.assertFalse(writer.is_alive())
                self.assertEqual(len(errors), 1)
            finally:
                sock.shutdown(gateway.socket.SHUT_RDWR)
                writer.join(1)

class Stop:
    def __init__(self):
        self.event = threading.Event()
        self.waits = []

    def is_set(self):
        return self.event.is_set()

    def set(self):
        self.event.set()

    def wait(self, delay):
        # Gateway backoff is recorded without sleeping; monitor uses its own Event.
        self.waits.append(delay)
        return self.is_set()

def event(op, data=None, **kwargs):
    return {'op': op, 'd': data, **kwargs}

HELLO = event(10, {'heartbeat_interval': 1000})
READY = event(0, {'session_id': 'session', 'resume_gateway_url': 'wss://resume.example'}, t='READY', s=42)

class ScriptSocket:
    def __init__(self, *script):
        self.script = list(script)
        self.sent, self.closes = [], []
        self.closed = threading.Event()
        self.beat = threading.Event()

    def recv(self):
        if not self.script:
            raise AssertionError('WebSocket script exhausted')
        result = self.script.pop(0)
        if callable(result):
            result = result()
        if isinstance(result, Exception):
            raise result
        return json.dumps(result)

    def send_text(self, text):
        payload = json.loads(text)
        self.sent.append(payload)
        if payload['op'] == 1:
            self.beat.set()

    def close(self, code=1000):
        self.closes.append(code)
        self.closed.set()

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.stop = Stop()
        self.logs, self.dispatches, self.urls = [], [], []
        self.client = gateway.Gateway(TOKEN, lambda t, d: self.dispatches.append((t, d)), 'wss://initial.example?old=value', log=self.logs.append)
        self.enterContext(patch.object(gateway.random, 'random', return_value=0.5))
        self.enterContext(patch.object(gateway.random, 'uniform', return_value=3))
        self.clock = 0
        self.enterContext(patch.object(gateway.time, 'monotonic', side_effect=lambda: self.clock))

    def finish(self):
        self.stop.set()
        return event(11)

    def run_script(self, *sockets):
        pending = list(sockets)

        def connect(url, timeout):
            self.urls.append(url)
            self.assertEqual(timeout, 10)
            if not pending:
                raise AssertionError('Unexpected reconnect')
            result = pending.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(gateway.WebSocket, 'connect', side_effect=connect):
            self.client.run(self.stop)
        self.assertFalse(pending)

    def test_identify_intents_properties_dispatch_and_immediate_heartbeat_seq(self):
        ws = ScriptSocket(HELLO, READY, event(1), event(11), event(0, {'id': 'interaction'}, t='INTERACTION_CREATE', s=43), self.finish)
        self.run_script(ws)
        self.assertEqual(ws.sent[0], {'op': 2, 'd': {'token': TOKEN, 'intents': 0,
                         'properties': {'os': 'windows', 'browser': 'ai-agents', 'device': 'ai-agents'}}})
        self.assertEqual(ws.sent[1], {'op': 1, 'd': 42})
        self.assertEqual(self.client.last_seq, 43)
        self.assertEqual([t for t, _ in self.dispatches], ['READY', 'INTERACTION_CREATE'])
        self.assertEqual(self.urls, ['wss://initial.example?v=10&encoding=json'])
        self.assertEqual(ws.closes, [1000])

    def test_op7_resume_uses_resume_url_and_last_sequence(self):
        first = ScriptSocket(HELLO, READY, event(7))
        second = ScriptSocket(HELLO, event(0, {}, t='RESUMED', s=44), self.finish)
        self.run_script(first, second)
        self.assertEqual(second.sent[0], {'op': 6, 'd': {'token': TOKEN, 'session_id': 'session', 'seq': 42}})
        self.assertEqual(self.urls[1], 'wss://resume.example?v=10&encoding=json')
        self.assertEqual(first.closes, [4000])
        self.assertEqual(self.stop.waits, [])

    def test_invalid_session_false_waits_and_identifies_true_resumes(self):
        for resumable in (False, True):
            with self.subTest(resumable=resumable):
                self.stop = Stop()
                self.urls.clear()
                self.client = gateway.Gateway(TOKEN, lambda t, d: None, 'wss://initial.example', log=self.logs.append)
                first = ScriptSocket(HELLO, READY, event(9, resumable))
                second = ScriptSocket(HELLO, self.finish)
                self.run_script(first, second)
                self.assertEqual(second.sent[0]['op'], 6 if resumable else 2)
                self.assertEqual(self.stop.waits, [] if resumable else [3])
                self.assertIn('resume.example' if resumable else 'initial.example', self.urls[1])
                if not resumable:
                    self.assertIsNone(self.client.last_seq)

    def test_fatal_closes_never_retry_and_do_not_leak_reason(self):
        for code in gateway.FATAL_CODES:
            with self.subTest(code=code):
                ws = ScriptSocket(HELLO, gateway.WebSocketClosed(code, TOKEN))
                with self.assertRaises(gateway.GatewayFatal) as raised:
                    self.run_script(ws)
                self.assertIn(str(code), str(raised.exception))
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertEqual(self.stop.waits, [])
                self.assertEqual(self.logs, [])
        self.assertEqual(len(self.urls), len(gateway.FATAL_CODES))

    def test_zombie_reconnects_and_resumes(self):
        first = ScriptSocket()

        def first_beat():
            self.clock = 0.5
            self.assertTrue(first.beat.wait(2), 'heartbeat thread did not send first beat')
            return event(0, {}, t='INTERACTION_CREATE', s=43)

        def zombie():
            self.clock = 1.5
            self.assertTrue(first.closed.wait(2), 'heartbeat thread did not close zombie')
            return gateway.WebSocketClosed(1000)

        first.script = [HELLO, READY, first_beat, zombie]
        second = ScriptSocket(HELLO, self.finish)
        self.run_script(first, second)
        self.assertEqual(first.sent[1], {'op': 1, 'd': 42})
        self.assertEqual(second.sent[0], {'op': 6, 'd': {'token': TOKEN, 'session_id': 'session', 'seq': 43}})
        self.assertTrue(all(code == 4000 for code in first.closes))
        self.assertTrue(any('ACK missing' in line for line in self.logs))
        self.assertEqual(self.stop.waits, [1])

    def test_ack_allows_next_scheduled_heartbeat_with_latest_seq(self):
        ws = ScriptSocket()

        def first_beat():
            self.clock = 0.5
            self.assertTrue(ws.beat.wait(2))
            ws.beat.clear()
            return event(11)

        def next_beat():
            self.clock = 1.5
            self.assertTrue(ws.beat.wait(2))
            return event(11)

        ws.script = [HELLO, READY, first_beat, event(0, {}, t='INTERACTION_CREATE', s=43), next_beat, self.finish]
        self.run_script(ws)
        self.assertEqual(ws.sent[1:], [{'op': 1, 'd': 42}, {'op': 1, 'd': 43}])
        self.assertEqual(self.logs, [])

    def test_backoff_caps_and_resets_after_ready_and_resumed(self):
        failures = [OSError('offline')] * 8
        ready = ScriptSocket(HELLO, READY, OSError('offline'))
        failure = ScriptSocket(HELLO, OSError('offline'))
        resumed = ScriptSocket(HELLO, event(0, {}, t='RESUMED', s=44), OSError('offline'))
        final = ScriptSocket(HELLO, self.finish)
        self.run_script(*failures, ready, failure, resumed, final)
        self.assertEqual(self.stop.waits, [1, 2, 4, 8, 16, 32, 60, 60, 1, 2, 1])

    def test_invalid_sequence_and_expired_session_identify_again(self):
        for code in (4007, 4009):
            with self.subTest(code=code):
                self.stop = Stop()
                self.client.session_id = self.client.resume_gateway_url = self.client.last_seq = None
                first = ScriptSocket(HELLO, READY, gateway.WebSocketClosed(code, 'session invalid'))
                second = ScriptSocket(HELLO, self.finish)
                self.run_script(first, second)
                self.assertEqual(second.sent[0]['op'], 2)
                self.assertIn('initial.example', self.urls[-1])
                self.assertEqual(self.stop.waits, [1])

    def test_hello_required_and_invalid_intervals_retry_without_identifying(self):
        for hello in (event(0), event(10, {'heartbeat_interval': 0}), event(10, {'heartbeat_interval': float('inf')})):
            with self.subTest(hello=hello):
                self.stop = Stop()
                bad = ScriptSocket(hello)
                good = ScriptSocket(HELLO, event(1), self.finish)
                self.run_script(bad, good)
                self.assertEqual(bad.sent, [])
                self.assertEqual(good.sent[1], {'op': 1, 'd': None})
                self.assertEqual(self.stop.waits, [1])

    def test_logger_failure_exception_text_redacts_token(self):
        self.client.log = Mock(side_effect=RuntimeError(TOKEN))
        with self.assertRaises(RuntimeError) as raised:
            self.run_script(OSError(TOKEN))
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIn('Gateway logger failed', str(raised.exception))
        self.assertNotIn(TOKEN, self.client.log.call_args.args[0])

    def test_callback_exception_survives_and_all_error_logs_redact_credentials(self):
        def callback(name, data):
            self.dispatches.append(name)
            if name == 'READY':
                raise ValueError('callback ' + TOKEN + ' Authorization: Bot ' + TOKEN)

        self.client.on_dispatch = callback
        first = ScriptSocket(HELLO, READY, event(0, {}, t='INTERACTION_CREATE', s=43), gateway.WebSocketClosed(1006, TOKEN))
        second = ScriptSocket(HELLO, self.finish)
        self.run_script(OSError(TOKEN), first, second)
        self.assertEqual(self.dispatches, ['READY', 'INTERACTION_CREATE'])
        self.assertEqual(len(self.logs), 3)
        self.assertTrue(any('ValueError: callback [redacted]' in line for line in self.logs))
        self.assertNotIn(TOKEN, '\n'.join(self.logs))
        self.assertNotIn('Authorization', '\n'.join(self.logs))

    def test_stop_unblocks_receive_and_closes_normally(self):
        ws = ScriptSocket()

        def blocked_receive():
            self.stop.set()
            self.assertTrue(ws.closed.wait(2))
            return gateway.WebSocketClosed(1000)

        ws.script = [HELLO, blocked_receive]
        self.run_script(ws)
        self.assertTrue(all(code == 1000 for code in ws.closes))
        self.assertEqual(self.logs, [])
        self.assertEqual(self.stop.waits, [])

    def test_stop_before_connect_and_during_backoff(self):
        self.stop.set()
        self.run_script()
        self.assertEqual(self.urls, [])
        self.stop = Stop()

        def wait(delay):
            self.stop.waits.append(delay)
            self.stop.set()
            return True

        self.stop.wait = wait
        self.run_script(OSError('offline'))
        self.assertEqual(self.stop.waits, [1])

    def test_stop_interrupts_blocked_identify_heartbeats_and_close(self):
        for phase in ('identify', 'requested heartbeat', 'scheduled heartbeat', 'close'):
            with self.subTest(phase=phase):
                self.stop = Stop()
                self.clock = 0
                dispatched = threading.Event()
                errors = []
                events = [HELLO]
                if phase != 'identify':
                    events.append(READY)
                if phase == 'requested heartbeat':
                    events.append(event(1))
                block_op = 2 if phase == 'identify' else 'close' if phase == 'close' else 1
                sock = BlockingSocket(b''.join(frame(json.dumps(e).encode()) for e in events), block_op)
                ws = gateway.WebSocket(sock)

                def on_dispatch(name, data):
                    dispatched.set()
                    if phase == 'scheduled heartbeat':
                        self.clock = 0.5

                client = gateway.Gateway(TOKEN, on_dispatch, 'wss://offline.example', log=self.logs.append)

                def run():
                    try:
                        client.run(self.stop)
                    except Exception as exc:
                        errors.append(exc)

                with patch.object(gateway.WebSocket, 'connect', return_value=ws) as connect, patch.object(gateway, 'CLOSE_TIMEOUT', 0.05):
                    worker = threading.Thread(target=run, daemon=True)
                    worker.start()
                    try:
                        if phase == 'close':
                            self.assertTrue(dispatched.wait(1))
                            self.stop.set()
                        self.assertTrue(sock.blocked.wait(1), 'probe did not enter blocked sendall')
                        self.stop.set()
                        worker.join(1)
                        self.assertFalse(worker.is_alive(), 'run did not return after stop')
                        self.assertTrue(sock.shutdown_called.is_set(), 'socket shutdown was not attempted')
                        self.assertEqual(errors, [])
                        self.assertEqual(self.logs, [])
                        connect.assert_called_once()
                        if phase == 'close':
                            self.assertEqual(unmask(sock.sent[-1]), (8, struct.pack('!H', 1000)))
                    finally:
                        self.stop.set()
                        sock.shutdown(gateway.socket.SHUT_RDWR)
                        worker.join(1)

    def test_stalled_scheduled_heartbeat_write_reconnects_with_resume(self):
        sock = BlockingSocket(frame(json.dumps(HELLO).encode()) + frame(json.dumps(READY).encode()), block_op=1)
        ws = gateway.WebSocket(sock)

        def on_dispatch(name, data):
            if name == 'READY':
                self.clock = 0.5

        self.client.on_dispatch = on_dispatch
        second = ScriptSocket(HELLO, self.finish)
        with patch.object(gateway, 'WRITE_TIMEOUT', 0.05), patch.object(gateway, 'CLOSE_TIMEOUT', 0.05):
            self.run_script(ws, second)
        self.assertTrue(sock.blocked.is_set())
        self.assertTrue(sock.shutdown_called.is_set())
        self.assertEqual(second.sent[0], {'op': 6, 'd': {'token': TOKEN, 'session_id': 'session', 'seq': 42}})
        self.assertEqual(self.stop.waits, [1])
        self.assertNotIn(TOKEN, '\n'.join(self.logs))

if __name__ == '__main__':
    unittest.main()
