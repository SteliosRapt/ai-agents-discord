"""Standalone Discord webhook client. Python standard library only."""
import argparse
import ctypes
import getpass
import json
import mimetypes
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
SECRET = ROOT / 'webhook.dpapi'

class TooLarge(RuntimeError):
    pass

def crypt(data, protect):
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buf), Blob()
    api = ctypes.windll.crypt32
    if protect:
        ok = api.CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output))
    else:
        ok = api.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output))
    if not ok:
        raise RuntimeError('Windows credential access failed; run setup under this Windows account.')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))

def webhook():
    value = os.environ.get('DISCORD_WEBHOOK_URL')
    if not value:
        value = crypt(bytes.fromhex(SECRET.read_text(encoding='utf-8-sig').strip()), False).decode('utf-16-le')
    if not re.fullmatch(r'https://(?:discord.com|discordapp.com)/api/webhooks/\d+/[A-Za-z0-9_-]+', value):
        raise RuntimeError('Invalid Discord webhook URL')
    return value

def multipart(payload, path):
    boundary = uuid.uuid4().hex
    paths = [path] if isinstance(path, Path) else list(path)
    # JSON quoting handles quotes and backslashes in ordinary filenames.
    filenames = [p.name.replace('\r', '').replace('\n', '') for p in paths]
    payload['attachments'] = [{'id': i, 'filename': name} for i, name in enumerate(filenames)]
    data = (f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n'.encode()
            + json.dumps(payload).encode())
    for i, (p, filename) in enumerate(zip(paths, filenames)):
        data += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files[{i}]"; filename={json.dumps(filename)}\r\nContent-Type: {mimetypes.guess_type(filename)[0] or "application/octet-stream"}\r\n\r\n'.encode()
                 + p.read_bytes())
    data += f'\r\n--{boundary}--\r\n'.encode()
    return data, 'multipart/form-data; boundary=' + boundary

def send(message, file=None, limit_mib=20):
    if len(message) > 2000:
        raise ValueError('Discord message must be at most 2000 characters')
    payload = {'username': 'AI Agents', 'content': message, 'allowed_mentions': {'parse': []}}
    if file:
        file = Path(file)
        if file.stat().st_size > limit_mib * 1024**2:
            raise TooLarge('File exceeds configured upload budget')
        body, content_type = multipart(payload, file)
    else:
        body, content_type = json.dumps(payload).encode(), 'application/json'
    req = urllib.request.Request(webhook() + '?wait=true', data=body, headers={'Content-Type': content_type, 'User-Agent': 'AI AgentsToolkit/1.0'})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read())
            except ValueError:
                error = {}
            if exc.code == 413 or error.get('code') == 40005:
                raise TooLarge('Discord rejected the attachment size') from None
            if exc.code == 429 and attempt < 4:
                time.sleep(max(1, float(error.get('retry_after', 5))))
                continue
            raise RuntimeError(f'Discord returned HTTP {exc.code}') from None
        except urllib.error.URLError:
            # Delivery may have happened: do not automatically duplicate a timed-out POST.
            raise RuntimeError('Network failure; delivery is uncertain. Check Discord before retrying.') from None

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'send'])
    parser.add_argument('message', nargs='?', default='')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--limit-mib', type=float, default=20)
    args = parser.parse_args()
    if args.action == 'setup':
        value = getpass.getpass('Discord webhook URL (hidden): ').strip()
        if not re.fullmatch(r'https://(?:discord.com|discordapp.com)/api/webhooks/\d+/[A-Za-z0-9_-]+', value):
            raise ValueError('Invalid Discord webhook URL')
        SECRET.write_text(crypt(value.encode('utf-16-le'), True).hex(), encoding='utf-8')
        print('Saved encrypted webhook for this Windows account.')
    else:
        result = send(args.message, args.file, args.limit_mib)
        print('Delivered message:', result['id'])

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc))
        raise SystemExit(1)
