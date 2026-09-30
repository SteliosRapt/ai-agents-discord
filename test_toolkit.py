import contextlib
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException, BadStatusLine
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import urllib.error
import discord_client as client
import send_video

class Tests(unittest.TestCase):
    def test_crypto(self):
        raw = 'private test value'.encode('utf-16-le')
        self.assertEqual(client.crypt(client.crypt(raw, True), False), raw)

    def test_multipart(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'report.txt'; p.write_bytes(b'hello')
            body, kind = client.multipart({'content': 'test'}, p)
            self.assertIn(b'name="files[0]"', body)
            self.assertIn(b'hello', body)
            self.assertIn('boundary=', kind)

    def test_rejected_size(self):
        error = urllib.error.HTTPError('https://example.invalid', 400, '', {}, io.BytesIO(b'{"code":40005}'))
        with patch.object(client, 'webhook', return_value='https://example.invalid'), patch.object(client.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(client.TooLarge): client.send('test')

    def test_rate_limit(self):
        error = urllib.error.HTTPError('https://example.invalid', 429, '', {}, io.BytesIO(b'{"retry_after":2}'))
        with patch.object(client, 'webhook', return_value='https://example.invalid'), patch.object(client.urllib.request, 'urlopen', side_effect=[error, io.BytesIO(b'{"id":"ok"}')]), patch.object(client.time, 'sleep') as sleep:
            self.assertEqual(client.send('test')['id'], 'ok')
            sleep.assert_called_once_with(2)

    def test_raw_network_errors_during_open_and_read(self):
        private = 'offline-private-credential'
        for stage in ('open', 'read', 'error_read'):
            for error_type in (TimeoutError, OSError, HTTPException, BadStatusLine):
                with self.subTest(stage=stage, error_type=error_type):
                    error = error_type(private)
                    response = Mock()
                    response.read.side_effect = error
                    if stage == 'open':
                        kwargs = {'side_effect': error}
                    elif stage == 'read':
                        kwargs = {'return_value': contextlib.nullcontext(response)}
                    else:
                        kwargs = {'side_effect': urllib.error.HTTPError('https://example.invalid', 429, private, {}, response)}
                    with patch.object(client, 'webhook', return_value='https://example.invalid'), patch.object(client.urllib.request, 'urlopen', **kwargs) as http, patch.object(client.time, 'sleep') as sleep:
                        with self.assertRaisesRegex(RuntimeError, 'delivery uncertain') as exc:
                            client.send('test')
                        self.assertNotIn(private, str(exc.exception))
                        self.assertNotIn('https://example.invalid', str(exc.exception))
                        http.assert_called_once()
                        sleep.assert_not_called()

    def test_user_agent(self):
        with patch.object(client, 'webhook', return_value='https://example.invalid'), patch.object(client.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"id":"ok"}')) as http:
            client.send('test')
        self.assertEqual(http.call_args.args[0].get_header('User-agent'), 'AIAgentsToolkit/1.0')

    def test_video_tiers(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'video.mp4'; p.write_bytes(b'original')
            descriptions = []
            def post(message, *args):
                descriptions.append(message)
                if len(descriptions) < 5: raise client.TooLarge()
                return {'id': 'ok'}
            def encode(args, **kwargs): Path(args[-1]).write_bytes(b'compressed')
            with patch('sys.argv', ['send_video.py', str(p)]), patch.object(send_video, 'send', side_effect=post), patch.object(send_video.shutil, 'which', return_value='tool'), patch.object(send_video.subprocess, 'check_output', return_value=b'{"format":{"duration":"36"}}'), patch.object(send_video.subprocess, 'run', side_effect=encode):
                send_video.main()
            self.assertIn('240p', descriptions[-1])
            self.assertEqual(p.read_bytes(), b'original')

    def test_concurrent_video_sends_have_exclusive_output_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            sources = [Path(directory) / 'video.mp4', Path(directory) / 'video.mov']
            for source in sources:
                source.write_bytes(b'original')
            barrier = threading.Barrier(3)
            outputs = []
            def post(message, path, limit):
                if path in sources:
                    raise client.TooLarge()
                self.assertEqual(path.read_bytes(), b'compressed')
                return {'id': 'ok'}
            def encode(args, **kwargs):
                output = Path(args[-1])
                outputs.append(output)
                barrier.wait(timeout=5)
                output.write_bytes(b'compressed')
            with patch.object(send_video.shutil, 'which', return_value='tool'), \
                    patch.object(send_video.subprocess, 'check_output', return_value=b'{"format":{"duration":"36"}}'), \
                    patch.object(send_video.subprocess, 'run', side_effect=encode), ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(send_video.deliver_video, source, 'video', post)
                           for source in (sources[0], sources[1], sources[0])]
                self.assertEqual([future.result(timeout=10) for future in futures], [{'id': 'ok'}] * 3)
            self.assertEqual(len({output.parent for output in outputs}), 3)
            self.assertTrue(all(output.parent.parent.name == 'video_discord_copies' for output in outputs))
            self.assertTrue(all((output.parent / 'encoding.log').is_file() for output in outputs))
            self.assertTrue(all(source.read_bytes() == b'original' for source in sources))

if __name__ == '__main__': unittest.main()
