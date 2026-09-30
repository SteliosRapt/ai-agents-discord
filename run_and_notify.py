"""Run a command with a full log and live Discord webhook status."""
import argparse
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

def read_output(stream, output, logfile, tail, last):
    """Keep blocking pipe reads away from the progress timer."""
    try:
        for line in stream:
            sys.stdout.write(line)
            sys.stdout.flush()
            logfile.write(line)
            logfile.flush()
            tail.append(line)
            if line.strip():
                last[0] = line.strip()[:200]
    except Exception as exc:
        output.put(exc)
    finally:
        output.put(None)

def report_failure(action, exc):
    print(f'Discord {action} failed: {exc}', file=sys.stderr)

def run_job(command, name, every=60, log=None, *, clock=time.monotonic, sleep=time.sleep):
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
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors='replace', bufsize=1)
        output = queue.Queue()
        reader = threading.Thread(target=read_output, args=(process.stdout, output, logfile, tail, last), daemon=True)
        reader.start()
        stream_closed = False
        try:
            while True:
                for _ in range(output.qsize()):
                    event = output.get_nowait()
                    if event is None:
                        stream_closed = True
                    else:
                        raise event
                code = process.poll()
                if code is not None and stream_closed:
                    break
                now = clock()
                if code is None and now >= next_update:
                    if message_id is not None:
                        try:
                            edit(message_id, f'\u25b6 {name} running for {elapsed_text(now - started)} \u00b7 last: {last[0]}')
                        except Exception as exc:
                            report_failure('progress edit', exc)
                    next_update = clock() + every
                sleep(0.1)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait()
            reader.join()
            process.stdout.close()
    elapsed = elapsed_text(clock() - started)
    final = (f'\u2705 {name} finished in {elapsed}' if code == 0
             else f'\u274c {name} failed (exit {code}) after {elapsed}')
    if message_id is not None:
        try:
            edit(message_id, final)
        except Exception as exc:
            report_failure('final edit', exc)
    try:
        if code == 0:
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
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not command:
        p.error('Supply a command after --')
    return run_job(command, a.name, a.every, a.log, clock=clock, sleep=sleep)

if __name__ == '__main__':
    sys.exit(main())
