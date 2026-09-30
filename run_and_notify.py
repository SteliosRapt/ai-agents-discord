"""Run a command with a full log and live Discord webhook status."""
import argparse
import codecs
from collections import deque
from datetime import datetime, timezone
import math
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import agent_state
from discord_client import edit, send
from redaction import redact

def safe_name(name):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_-')[:100] or 'job'

def elapsed_text(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}h {minutes}m {seconds}s'
    if minutes:
        return f'{minutes}m {seconds}s'
    return f'{seconds}s'

def interval(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 15:
        raise argparse.ArgumentTypeError('--every must be at least 15 seconds and finite')
    return seconds

def console_write(text, stream):
    encoding = getattr(stream, 'encoding', None)
    if encoding:
        text = text.encode(encoding, errors='replace').decode(encoding, errors='replace')
    try:
        stream.write(text)
    except UnicodeEncodeError as exc:
        stream.write(text.encode(exc.encoding, errors='replace').decode(exc.encoding, errors='replace'))
    stream.flush()

def read_output(stream, output, logfile, tail, last, encoding='utf-8', stop=None, lock=None):
    """Keep blocking pipe reads away from the progress timer."""
    stop = stop if stop is not None else threading.Event()
    lock = lock if lock is not None else threading.Lock()
    try:
        for line in stream:
            line = redact(line.decode(encoding, errors='replace'))
            with lock:
                if stop.is_set():
                    break
                logfile.write(line)
                logfile.flush()
                tail.append(line)
                if line.strip():
                    last[0] = line.strip()[:200]
            console_write(line, sys.stdout)
    except Exception as exc:
        output.put(exc)
    finally:
        try:
            stream.close()
        except Exception as exc:
            output.put(exc)
        output.put(None)

def report_failure(action, exc):
    console_write(redact(f'Discord {action} failed: {exc}\n'), sys.stderr)

def kill_tree(process):
    result = subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f'taskkill failed (exit {result.returncode}): ' +
                           redact(result.stdout.decode('utf-8', errors='replace')))

def run_job(command, name, every=60, log=None, *, encoding='utf-8', clock=time.monotonic, sleep=time.sleep):
    name = redact(name)
    log = Path(log) if log is not None else agent_state.ROOT / 'jobs' / (
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '_' + safe_name(name) + '.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    tail, last = deque(maxlen=60), ['']
    with log.open('w', encoding='utf-8', newline='') as logfile:
        message_id = None
        try:
            message_id = send(f'\u25b6 {name} started')['id']
        except Exception as exc:
            report_failure('start post', exc)
        started = clock()
        next_update = started + every
        final = None
        launch_failed = False
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        except Exception as exc:
            code = 1
            launch_failed = True
            final = redact(f'\u274c {name} failed to start: {exc}')
            logfile.write(final + '\n')
            logfile.flush()
        else:
            output, stop, lock = queue.Queue(), threading.Event(), threading.Lock()
            reader = threading.Thread(target=read_output,
                                      args=(process.stdout, output, logfile, tail, last, encoding, stop, lock), daemon=True)
            code, abnormal, reader_started = 1, False, False

            def note(text):
                text = redact(text) + '\n'
                with lock:
                    logfile.write(text)
                    logfile.flush()
                    tail.append(text)

            def cleanup_tree():
                nonlocal code, abnormal
                for attempt in range(2):
                    try:
                        kill_tree(process)
                        return
                    except KeyboardInterrupt:
                        code, abnormal = 130, True
                        if attempt == 0:
                            note('Process tree cleanup interrupted by Ctrl+C; retrying')
                        else:
                            error = 'Interrupted twice by Ctrl+C; tree termination unconfirmed'
                            note(f'Process tree cleanup failed: {error}')
                            report_failure('process tree cleanup', error)
                    except Exception as exc:
                        note(f'Process tree cleanup failed: {exc}')
                        report_failure('process tree cleanup', exc)
                        return

            try:
                reader.start()
                reader_started = True
                while True:
                    for _ in range(output.qsize()):
                        event = output.get_nowait()
                        if event is not None:
                            raise event
                    code = process.poll()
                    if code is not None:
                        break
                    now = clock()
                    if now >= next_update:
                        if message_id is not None:
                            try:
                                edit(message_id, f'\u25b6 {name} running for {elapsed_text(now - started)} \u00b7 last: {last[0]}')
                            except Exception as exc:
                                report_failure('progress edit', exc)
                        next_update = clock() + every
                    sleep(0.1)
            except KeyboardInterrupt:
                code, abnormal = 130, True
                note('Job interrupted by Ctrl+C')
            except Exception as exc:
                code, abnormal = 1, True
                note(f'Job output or monitoring failed: {exc}')
                report_failure('job monitoring', exc)
            finally:
                if abnormal or code != 0:
                    cleanup_tree()
                try:
                    process.wait(timeout=10)
                except KeyboardInterrupt:
                    code, abnormal = 130, True
                    note('Job cleanup interrupted by Ctrl+C')
                    cleanup_tree()
                except subprocess.TimeoutExpired as exc:
                    cleanup_tree()
                    note(f'Root process did not exit during cleanup: {exc}')
                    code = code or 1
                drain_interrupted = False
                try:
                    if reader_started:
                        reader.join(timeout=10)
                    else:
                        process.stdout.close()
                except KeyboardInterrupt:
                    code, abnormal, drain_interrupted = 130, True, True
                    note('Output drain interrupted by Ctrl+C')
                    cleanup_tree()
                if reader.is_alive():
                    # Never close a pipe in another thread while it holds the
                    # buffered read lock. The daemon reader owns pipe closure.
                    with lock:
                        stop.set()
                    if not drain_interrupted:
                        note('Output pipe still open after 10s; stopping process tree and output drain')
                        cleanup_tree()
                for _ in range(output.qsize()):
                    event = output.get_nowait()
                    if event is not None:
                        note(f'Job output failed: {event}')
                        report_failure('job output', event)
                        if not abnormal:
                            code = 1
                            cleanup_tree()
    elapsed = elapsed_text(clock() - started)
    final = final or (f'\u2705 {name} finished in {elapsed}' if code == 0
                      else f'\u274c {name} failed (exit {code}) after {elapsed}')
    if message_id is not None:
        try:
            edit(message_id, final)
        except Exception as exc:
            report_failure('final edit', exc)
    try:
        if code == 0 or launch_failed:
            send(final)
        else:
            # Beside the log, a temporary folder keeps the attachment name stable
            # without overwriting a concurrent job's tail or an existing file.
            with tempfile.TemporaryDirectory(prefix='job_tail_', dir=log.parent) as folder:
                filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)[:100] + '_tail.txt'
                attachment = Path(folder) / filename
                attachment.write_text(''.join(tail), encoding='utf-8', newline='')
                send(final, attachment)
    except Exception as exc:
        report_failure('final post', exc)
    return code

def main(argv=None, *, clock=time.monotonic, sleep=time.sleep):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', required=True)
    p.add_argument('--every', type=interval, default=60)
    p.add_argument('--log', type=Path)
    p.add_argument('--encoding', default='utf-8', help='Child output encoding (default: utf-8)')
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not command:
        p.error('Supply a command after --')
    try:
        codecs.lookup(a.encoding)
    except LookupError:
        p.error('Unknown output encoding')
    return run_job(command, a.name, a.every, a.log, encoding=a.encoding, clock=clock, sleep=sleep)

if __name__ == '__main__':
    sys.exit(main())
