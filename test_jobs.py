"""Offline webhook edit and live job status tests."""
import contextlib
from http.client import HTTPException
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import discord_client as client
import run_and_notify as jobs

class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.url = 'https://discord.com/api/webhooks/123/offline-private-credential'
        self.enterContext(patch.object(client, 'webhook', return_value=self.url))

    def test_edit_endpoint_payload_and_returned_message(self):
        result = {'id': '456', 'content': 'Updated @everyone'}
        with patch.object(client.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(result).encode())) as http:
            self.assertEqual(client.edit('456', result['content']), result)
        request = http.call_args.args[0]
        self.assertEqual(request.get_method(), 'PATCH')
        self.assertEqual(request.full_url, self.url + '/messages/456')
        self.assertEqual(json.loads(request.data), {'content': result['content'], 'allowed_mentions': {'parse': []}})
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(request.get_header('User-agent'), 'AIAgentsToolkit/1.0')

    def test_send_waits_and_returns_message_without_mentions(self):
        with patch.object(client.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"id":"456"}')) as http:
            self.assertEqual(client.send('Started'), {'id': '456'})
        request = http.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.full_url, self.url + '?wait=true')
        self.assertEqual(json.loads(request.data)['allowed_mentions'], {'parse': []})

    def test_edit_validates_content_and_id_before_network_access(self):
        with patch.object(client.urllib.request, 'urlopen') as http:
            for message_id, content in [('1', 'x' * 2001), ('../1', 'ok'), ('', 'ok')]:
                with self.assertRaises(ValueError):
                    client.edit(message_id, content)
            http.assert_not_called()

    def test_uncertain_edit_failure_never_retries_or_exposes_url(self):
        for stage in ('open', 'read', 'error_read'):
            for error_type in (urllib.error.URLError, TimeoutError, OSError, HTTPException):
                with self.subTest(stage=stage, error_type=error_type):
                    response = Mock()
                    response.read.side_effect = error_type(self.url)
                    if stage == 'open':
                        kwargs = {'side_effect': error_type(self.url)}
                    elif stage == 'read':
                        kwargs = {'return_value': contextlib.nullcontext(response)}
                    else:
                        kwargs = {'side_effect': urllib.error.HTTPError(self.url, 429, self.url, {}, response)}
                    with patch.object(client.urllib.request, 'urlopen', **kwargs) as http, patch.object(client.time, 'sleep') as sleep:
                        with self.assertRaisesRegex(RuntimeError, 'delivery uncertain') as error:
                            client.edit('456', 'Updated')
                        self.assertNotIn(self.url, str(error.exception))
                        self.assertNotIn('offline-private-credential', str(error.exception))
                        self.assertTrue(error.exception.__suppress_context__)
                        http.assert_called_once()
                        sleep.assert_not_called()

    def test_edit_retries_only_explicit_rate_limits_and_bounds_retries(self):
        errors = [urllib.error.HTTPError(self.url, 429, self.url, {}, io.BytesIO(b'{"retry_after":2}')) for _ in range(5)]
        with patch.object(client.urllib.request, 'urlopen', side_effect=errors) as http, patch.object(client.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 429') as error:
                client.edit('456', 'Updated')
            self.assertNotIn(self.url, str(error.exception))
            self.assertEqual(http.call_count, 5)
            self.assertEqual([call.args for call in sleep.call_args_list], [(2,)] * 4)
        error = urllib.error.HTTPError(self.url, 429, '', {}, io.BytesIO(b'{"retry_after":3}'))
        with patch.object(client.urllib.request, 'urlopen', side_effect=[error, io.BytesIO(b'{"id":"456"}')]), patch.object(client.time, 'sleep') as sleep:
            self.assertEqual(client.edit('456', 'Updated'), {'id': '456'})
            sleep.assert_called_once_with(3)

    def test_http_errors_are_redacted_and_not_retried(self):
        for status, body, error_type in [(401, json.dumps({'message': self.url}).encode(), RuntimeError),
                                         (413, b'{}', client.TooLarge),
                                         (400, b'{"code":40005}', client.TooLarge),
                                         (500, b'[]', RuntimeError),
                                         (500, b'not json', RuntimeError),
                                         (429, b'{"retry_after":"private"}', RuntimeError)]:
            with self.subTest(status=status, body=body):
                error = urllib.error.HTTPError(self.url, status, self.url, {}, io.BytesIO(body))
                with patch.object(client.urllib.request, 'urlopen', side_effect=error) as http, patch.object(client.time, 'sleep') as sleep:
                    with self.assertRaises(error_type) as raised:
                        client.edit('456', 'Updated')
                    self.assertNotIn(self.url, str(raised.exception))
                    http.assert_called_once()
                    sleep.assert_not_called()

class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 10)

class ImmediateThread:
    """Run a mocked, nonblocking pipe reader synchronously for deterministic tests."""
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False

class JobTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs.agent_state, 'ROOT', self.root))
        self.enterContext(patch.object(jobs.threading, 'Thread', ImmediateThread))
        self.clock = Clock()
        self.events = []
        self.send = self.enterContext(patch.object(jobs, 'send', side_effect=self.post))
        self.edit = self.enterContext(patch.object(jobs, 'edit', side_effect=self.update))
        self.popen = self.enterContext(patch.object(jobs.subprocess, 'Popen'))
        self.taskkill = self.enterContext(patch.object(jobs.subprocess, 'run'))
        self.taskkill.return_value.returncode = 0

    def post(self, content, file=None):
        self.events.append(('post', content, Path(file) if file else None,
                            Path(file).read_text(encoding='utf-8') if file else None))
        return {'id': '456'}

    def update(self, message_id, content):
        self.events.append(('edit', message_id, content, self.clock()))

    def run_job(self, output='', code=0, duration=0, name='Build', every=15, log=None):
        self.process = Mock()
        self.process.stdout = io.BytesIO(output.encode('utf-8'))
        self.process.poll.side_effect = lambda: code if self.clock() >= duration else None
        self.process.wait.return_value = code
        self.popen.return_value = self.process
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            result = jobs.run_job(['tool', '--flag'], name, every, log, clock=self.clock, sleep=self.clock.sleep)
        return result, out.getvalue(), err.getvalue()

    def test_start_progress_final_edit_and_new_post(self):
        line = 'x' * 250
        result, out, err = self.run_job('first\n' + line + '\n \n', duration=31)
        self.assertEqual((result, out, err), (0, 'first\n' + line + '\n \n', ''))
        self.assertEqual(self.events[0][:2], ('post', '▶ Build started'))
        self.assertEqual(self.events[1], ('edit', '456', '▶ Build running for 15s · last: ' + line[:200], 15))
        self.assertEqual(self.events[2], ('edit', '456', '▶ Build running for 30s · last: ' + line[:200], 30))
        self.assertEqual(self.events[3], ('edit', '456', '✅ Build finished in 31s', 31))
        self.assertEqual(self.events[4], ('post', '✅ Build finished in 31s', None, None))
        self.popen.assert_called_once_with(['tool', '--flag'], stdout=jobs.subprocess.PIPE,
                                          stderr=jobs.subprocess.STDOUT, creationflags=jobs.subprocess.CREATE_NEW_PROCESS_GROUP)
        self.process.terminate.assert_not_called()
        self.assertTrue(self.process.stdout.closed)
        logs = list((self.root / 'jobs').glob('*_Build.log'))
        self.assertEqual(len(logs), 1)
        self.assertRegex(logs[0].name, r'^\d{8}T\d{12}Z_Build\.log$')
        self.assertEqual(logs[0].read_text(encoding='utf-8'), out)

    def test_child_credentials_are_redacted_in_every_output_destination(self):
        token = 'M' + 'a' * 23 + '.' + 'b' * 6 + '.' + 'c' * 27
        webhook = 'https://discord.com/api/webhooks/123/synthetic-job-secret'
        with patch.dict(os.environ, DISCORD_BOT_TOKEN=token, DISCORD_WEBHOOK_URL=webhook):
            result, out, err = self.run_job(token + ' ' + webhook + '\n', code=7, duration=16)
        self.assertEqual(result, 7)
        log = next((self.root / 'jobs').glob('*.log')).read_text(encoding='utf-8')
        destinations = [out, err, log, repr(self.events)]
        for value in destinations:
            self.assertNotIn(token, value)
            self.assertNotIn(webhook, value)
        self.assertEqual(out, '[redacted] [redacted]\n')
        self.assertEqual(log, out)
        self.assertEqual(self.events[1][2], '▶ Build running for 15s · last: [redacted] [redacted]')
        self.assertEqual(self.events[-1][3], out)

    def test_oem_bytes_and_cp1253_console_keep_complete_utf8_log(self):
        class Console(io.StringIO):
            encoding = 'cp1253'

            def write(console, text):
                self.assertEqual(log.read_text(encoding='utf-8'), expected)
                text.encode(console.encoding)  # A strict redirected Greek console.
                return super(Console, console).write(text)
        expected = '╬ Ελληνικά\n'
        log = self.root / 'oem.log'
        process = Mock(stdout=io.BytesIO(expected.encode('cp737')))
        process.poll.return_value = process.wait.return_value = 0
        self.popen.return_value = process
        with contextlib.redirect_stdout(Console()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            result = jobs.run_job(['tool'], 'OEM', log=log, encoding='cp737', clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual((result, err.getvalue()), (0, ''))
        self.assertEqual(out.getvalue(), '? Ελληνικά\n')
        self.assertEqual(log.read_bytes(), expected.encode('utf-8'))
        self.taskkill.assert_not_called()

    def test_invalid_utf8_bytes_are_replaced_without_losing_later_lines(self):
        process = Mock(stdout=io.BytesIO(b'bad \xff\ncomplete\n'))
        process.poll.return_value = process.wait.return_value = 0
        self.popen.return_value = process
        log = self.root / 'bytes.log'
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = jobs.run_job(['tool'], 'Bytes', log=log, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(result, 0)
        self.assertEqual(out.getvalue(), 'bad �\ncomplete\n')
        self.assertEqual(log.read_bytes(), out.getvalue().encode('utf-8'))

    def test_pipe_held_by_descendant_has_bounded_drain_and_final_status(self):
        joins, threads = [], []
        class HeldThread(ImmediateThread):
            def start(self):
                threads.append(self)

            def join(thread, timeout=None):
                joins.append(timeout)

            def is_alive(self):
                return True
        def limited_sleep(seconds):
            Clock.sleep(self.clock, seconds)
            if self.clock() > 11:
                raise RuntimeError('Root exited but completion is still blocked')
        with patch.object(jobs.threading, 'Thread', HeldThread), patch.object(self.clock, 'sleep', side_effect=limited_sleep):
            result, _, _ = self.run_job()
        self.assertEqual(joins, [10])
        self.assertEqual(result, 0)
        self.taskkill.assert_called_once_with(['taskkill', '/T', '/F', '/PID', str(self.process.pid)],
                                              stdout=jobs.subprocess.PIPE, stderr=jobs.subprocess.STDOUT,
                                              timeout=10, creationflags=jobs.subprocess.CREATE_NO_WINDOW)
        self.assertIn('Output pipe still open after 10s', next((self.root / 'jobs').glob('*.log')).read_text(encoding='utf-8'))
        self.assertEqual(self.events[-1][:2], ('post', '✅ Build finished in 0s'))
        # Closing a pipe from another thread can itself block on its read lock.
        self.assertFalse(self.process.stdout.closed)
        # Once the held pipe releases, the stopped reader must not write to the
        # already-closed log or mutate the final attachment tail.
        self.process.stdout.write(b'late descendant output\n')
        self.process.stdout.seek(0)
        threads[0].target(*threads[0].args)
        output = threads[0].args[1]
        self.assertIsNone(output.get_nowait())
        self.assertTrue(output.empty())
        self.assertNotIn('late descendant output', next((self.root / 'jobs').glob('*.log')).read_text(encoding='utf-8'))
        self.assertTrue(self.process.stdout.closed)

    def test_ctrl_c_kills_tree_and_reports_final_status(self):
        def interrupt(seconds):
            raise KeyboardInterrupt
        with patch.object(self.clock, 'sleep', side_effect=interrupt):
            result, _, _ = self.run_job('working\n', duration=100)
        self.assertEqual(result, 130)
        self.assertEqual(self.taskkill.call_args.args[0], ['taskkill', '/T', '/F', '/PID', str(self.process.pid)])
        self.assertEqual(self.events[-2][2], '❌ Build failed (exit 130) after 0s')
        self.assertEqual(self.events[-1][:2], ('post', '❌ Build failed (exit 130) after 0s'))

    def test_ctrl_c_during_pipe_drain_still_kills_tree_and_posts_final(self):
        class InterruptedThread(ImmediateThread):
            def start(self):
                pass

            def join(self, timeout=None):
                raise KeyboardInterrupt

            def is_alive(self):
                return True
        with patch.object(jobs.threading, 'Thread', InterruptedThread):
            result, _, _ = self.run_job()
        self.assertEqual(result, 130)
        self.taskkill.assert_called_once()
        self.assertEqual(self.events[-1][:2], ('post', '❌ Build failed (exit 130) after 0s'))
        self.assertFalse(self.process.stdout.closed)

    def test_ctrl_c_during_taskkill_retries_tree_cleanup_and_reports_final(self):
        class HeldThread(ImmediateThread):
            def start(self):
                pass

            def is_alive(self):
                return True
        for code, thread in ((0, HeldThread), (7, ImmediateThread)):
            with self.subTest(code=code):
                self.events.clear()
                self.taskkill.reset_mock()
                self.taskkill.side_effect = [KeyboardInterrupt(), Mock(returncode=0)]
                with patch.object(jobs.threading, 'Thread', thread):
                    result, _, err = self.run_job(code=code)
                self.assertEqual((result, err), (130, ''))
                self.assertEqual(self.taskkill.call_count, 2)
                self.assertEqual(self.taskkill.call_args_list[0], self.taskkill.call_args_list[1])
                self.assertEqual(self.taskkill.call_args.args[0],
                                 ['taskkill', '/T', '/F', '/PID', str(self.process.pid)])
                self.assertEqual(self.events[-2][2], '❌ Build failed (exit 130) after 0s')
                self.assertEqual(self.events[-1][:2], ('post', '❌ Build failed (exit 130) after 0s'))
                self.assertIn('Process tree cleanup interrupted by Ctrl+C; retrying', self.events[-1][3])

    def test_repeated_ctrl_c_during_taskkill_is_bounded_and_reports_final(self):
        class HeldThread(ImmediateThread):
            def start(self):
                pass

            def is_alive(self):
                return True
        self.taskkill.side_effect = KeyboardInterrupt
        with patch.object(jobs.threading, 'Thread', HeldThread):
            result, _, err = self.run_job()
        self.assertEqual(result, 130)
        self.assertEqual(self.taskkill.call_count, 2)
        self.assertIn('tree termination unconfirmed', err)
        self.assertIn('tree termination unconfirmed', self.events[-1][3])
        self.assertEqual(self.events[-2][2], '❌ Build failed (exit 130) after 0s')
        self.assertEqual(self.events[-1][:2], ('post', '❌ Build failed (exit 130) after 0s'))

    def test_output_reader_error_kills_tree_and_still_reports_redacted_failure(self):
        secret = 'synthetic-reader-secret'
        class BrokenPipe:
            def __iter__(self):
                raise OSError(secret)

            def close(self):
                pass
        process = Mock(stdout=BrokenPipe())
        process.poll.return_value = None
        process.wait.return_value = 1
        self.popen.return_value = process
        with patch.dict(os.environ, DISCORD_BOT_TOKEN=secret), contextlib.redirect_stderr(io.StringIO()) as err:
            result = jobs.run_job(['tool'], 'Build', clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(result, 1)
        self.taskkill.assert_called_once()
        self.assertNotIn(secret, err.getvalue())
        self.assertNotIn(secret, repr(self.events))
        self.assertIn('[redacted]', self.events[-1][3])
        self.assertEqual(self.events[-1][:2], ('post', '❌ Build failed (exit 1) after 0s'))

    def test_tree_cleanup_failure_is_redacted_and_does_not_skip_final_status(self):
        secret = 'synthetic-cleanup-secret'
        self.taskkill.side_effect = OSError(secret)
        with patch.dict(os.environ, DISCORD_BOT_TOKEN=secret):
            result, _, err = self.run_job(code=7)
        self.assertEqual(result, 7)
        self.assertIn('Process tree cleanup failed: [redacted]', self.events[-1][3])
        self.assertIn('Discord process tree cleanup failed: [redacted]', err)
        self.assertNotIn(secret, err + repr(self.events))

    def test_launch_error_edits_start_posts_failure_and_redacts_error(self):
        secret = 'synthetic-inherited-job-token'
        self.popen.side_effect = FileNotFoundError('missing executable ' + secret)
        with patch.dict(os.environ, DISCORD_BOT_TOKEN=secret), contextlib.redirect_stderr(io.StringIO()) as err:
            result = jobs.run_job(['missing'], 'Build', clock=self.clock, sleep=self.clock.sleep)
        final = '❌ Build failed to start: missing executable [redacted]'
        self.assertEqual(result, 1)
        self.assertEqual(self.events, [('post', '▶ Build started', None, None),
                                      ('edit', '456', final, 0), ('post', final, None, None)])
        self.assertNotIn(secret, err.getvalue())
        log = next((self.root / 'jobs').glob('*.log')).read_text(encoding='utf-8')
        self.assertEqual(log, final + '\n')
        self.taskkill.assert_not_called()

    def test_silent_command_still_updates_at_configured_interval(self):
        result, out, err = self.run_job(duration=46, every=20)
        self.assertEqual((result, out, err), (0, '', ''))
        self.assertEqual(self.events[1], ('edit', '456', '▶ Build running for 20s · last: ', 20))
        self.assertEqual(self.events[2], ('edit', '456', '▶ Build running for 40s · last: ', 40))
        self.assertEqual(len(self.events), 5)

    def test_reader_streams_each_line_before_waiting_for_the_next(self):
        out, log = io.StringIO(), io.StringIO()
        output, tail, last = jobs.queue.Queue(), jobs.deque(maxlen=60), ['']
        def lines():
            yield b'first\n'
            self.assertEqual(out.getvalue(), 'first\n')
            self.assertEqual(log.getvalue(), 'first\n')
            self.assertEqual(last[0], 'first')
            yield b' \n'
            self.assertEqual(last[0], 'first')
            yield b'second\n'
        with contextlib.redirect_stdout(out):
            jobs.read_output(lines(), output, log, tail, last)
        self.assertIsNone(output.get_nowait())
        self.assertTrue(output.empty())
        self.assertEqual(last[0], 'second')
        self.assertEqual(list(tail), ['first\n', ' \n', 'second\n'])
        self.assertEqual(log.getvalue(), out.getvalue())

    def test_failure_keeps_full_output_and_attaches_only_last_60_lines(self):
        log = self.root / 'nested' / 'full.log'
        lines = [f'line {i}\n' for i in range(90)] + ['partial final line']
        result, out, err = self.run_job(''.join(lines), code=7, duration=2, log=log)
        self.assertEqual((result, out, err), (7, ''.join(lines), ''))
        self.assertEqual(log.read_text(encoding='utf-8'), ''.join(lines))
        self.assertEqual(self.events[-2], ('edit', '456', '❌ Build failed (exit 7) after 2s', 2))
        final = self.events[-1]
        self.assertEqual(final[:2], ('post', '❌ Build failed (exit 7) after 2s'))
        self.assertEqual(final[2].name, 'Build_tail.txt')
        self.assertEqual(final[2].parent.parent, log.parent)
        self.assertEqual(final[3], ''.join(lines[-60:]))
        self.assertFalse(final[2].exists())
        self.assertEqual(list(log.parent.iterdir()), [log])

    def test_short_failure_tail_and_empty_failure_tail(self):
        for output in ('one\n\ntwo\n', ''):
            with self.subTest(output=output):
                result, out, err = self.run_job(output, code=9, name='My job')
                self.assertEqual((result, out, err), (9, output, ''))
                self.assertEqual(self.events[-1][3], output)
                self.assertEqual(self.events[-1][2].name, 'My job_tail.txt')

    def test_progress_error_is_reported_and_later_edits_and_final_post_continue(self):
        def update(message_id, content):
            if '15s' in content:
                raise RuntimeError('Network failure; delivery uncertain. Check Discord before retrying.')
            self.update(message_id, content)
        self.edit.side_effect = update
        result, out, err = self.run_job('working\n', duration=31)
        self.assertEqual((result, out), (0, 'working\n'))
        self.assertIn('Discord progress edit failed: Network failure; delivery uncertain', err)
        self.assertEqual(self.events[-1][:2], ('post', '✅ Build finished in 31s'))
        self.assertEqual(self.events[1][2], '▶ Build running for 30s · last: working')

    def test_final_edit_failure_does_not_skip_final_post(self):
        self.edit.side_effect = RuntimeError('Discord returned HTTP 403')
        result, _, err = self.run_job(code=8)
        self.assertEqual(result, 8)
        self.assertIn('Discord final edit failed: Discord returned HTTP 403', err)
        self.assertEqual(self.events[-1][:2], ('post', '❌ Build failed (exit 8) after 0s'))

    def test_final_post_failure_preserves_success_and_failure_exit_codes(self):
        for code in (0, 7):
            self.events.clear()
            def post(content, file=None):
                if 'started' in content:
                    return self.post(content, file)
                raise RuntimeError('Network failure; delivery uncertain. Check Discord before retrying.')
            self.send.side_effect = post
            result, _, err = self.run_job(code=code)
            self.assertEqual(result, code)
            self.assertIn('Discord final post failed: Network failure; delivery uncertain', err)
            self.assertEqual(len(self.events), 2)
            self.assertEqual(list((self.root / 'jobs').glob('job_tail_*')), [])

    def test_start_failure_is_reported_and_job_and_final_post_continue(self):
        def post(content, file=None):
            if 'started' in content:
                raise RuntimeError('Discord returned HTTP 403')
            return self.post(content, file)
        self.send.side_effect = post
        result, out, err = self.run_job('still ran\n', duration=16)
        self.assertEqual((result, out), (0, 'still ran\n'))
        self.assertIn('Discord start post failed: Discord returned HTTP 403', err)
        self.edit.assert_not_called()
        self.assertEqual(self.events[-1][:2], ('post', '✅ Build finished in 16s'))

    def test_cli_default_interval_and_explicit_log_command_and_interval(self):
        with patch.object(jobs, 'run_job', return_value=4) as run:
            self.assertEqual(jobs.main(['--name', 'Name', '--', 'tool', '--arg']), 4)
            self.assertEqual(run.call_args.args, (['tool', '--arg'], 'Name', 60, None))
            self.assertEqual(run.call_args.kwargs['encoding'], 'utf-8')
            self.assertEqual(jobs.main(['--name', 'Name', '--every', '15', '--log', str(self.root / 'custom.log'), '--', 'tool']), 4)
            self.assertEqual(run.call_args.args, (['tool'], 'Name', 15, self.root / 'custom.log'))
            self.assertEqual(jobs.main(['--name', 'Name', '--encoding', 'cp737', '--', 'tool']), 4)
            self.assertEqual(run.call_args.kwargs['encoding'], 'cp737')

    def test_cli_rejects_short_or_invalid_interval_and_missing_command(self):
        cases = [['--name', 'Job', '--'], ['--name', 'Job']]
        cases += [['--name', 'Job', '--encoding', 'no-such-encoding', '--', 'tool']]
        cases += [['--name', 'Job', '--every', value, '--', 'tool'] for value in ('14.9', '0', '-1', 'nan', 'inf', 'bad')]
        with patch.object(jobs, 'run_job') as run:
            for args in cases:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as exit:
                        jobs.main(args)
                    self.assertEqual(exit.exception.code, 2)
                    self.assertIn('error:', err.getvalue())
            run.assert_not_called()

    def test_default_log_name_is_safe_and_elapsed_is_readable(self):
        self.run_job(name='../../bad:name')
        logs = list((self.root / 'jobs').glob('*.log'))
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].name.endswith('_bad_name.log'))
        self.assertEqual(jobs.safe_name('...'), 'job')
        self.assertEqual(jobs.elapsed_text(3661), '1h 1m 1s')
        self.assertEqual(jobs.elapsed_text(61), '1m 1s')

    def test_script_entrypoint_exits_with_child_code(self):
        process = Mock()
        process.stdout = io.BytesIO(b'child output\n')
        process.poll.return_value = process.wait.return_value = 7
        self.popen.return_value = process
        with patch.object(client, 'send', side_effect=self.post), patch.object(client, 'edit', side_effect=self.update), patch('sys.argv', ['run_and_notify.py', '--name', 'Job', '--', 'tool']), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit:
                runpy.run_path(jobs.__file__, run_name='__main__')
        self.assertEqual(exit.exception.code, 7)

if __name__ == '__main__':
    unittest.main()
