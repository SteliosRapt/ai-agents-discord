"""Discord REST v10 bot client; credentials stay local and encrypted."""
import argparse
import getpass
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from discord_client import crypt, multipart, TooLarge

ROOT = Path(__file__).resolve().parent
SECRET = ROOT / 'bot_token.dpapi'
API = 'https://discord.com/api/v10'

def validate_token(value):
    if not value or not value.isascii() or any(c.isspace() or ord(c) < 33 for c in value):
        raise RuntimeError('Invalid bot credential; run py bot_client.py setup')
    return value

def bot_token():
    if 'DISCORD_BOT_TOKEN' in os.environ:
        return validate_token(os.environ['DISCORD_BOT_TOKEN'])
    if not SECRET.is_file():
        raise RuntimeError('Missing bot credential; run py bot_client.py setup')
    try:
        value = crypt(bytes.fromhex(SECRET.read_text(encoding='utf-8-sig').strip()), False).decode('utf-16-le')
    except Exception:
        raise RuntimeError('Cannot read bot credential; run py bot_client.py setup under this Windows account') from None
    return validate_token(value)

def check_content(content):
    if not isinstance(content, str) or len(content) > 2000:
        raise ValueError('Discord message must be text of at most 2000 characters')

def snowflake(value):
    value = str(value)
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('Discord id must contain digits only')
    return value

class BotClient:
    def __init__(self, guild_id):
        self.guild_id = snowflake(guild_id)

    def request(self, method, path, payload=None, files=()):
        """Make one REST request, retrying only explicit rate limits."""
        if payload is not None:
            payload = dict(payload)
            if 'content' in payload:
                check_content(payload['content'])
            payload['allowed_mentions'] = {'parse': []}
        files = tuple(Path(f) for f in files)
        if len(files) > 10:
            raise ValueError('At most 10 files per message')
        if files:
            body, kind = multipart(payload or {'allowed_mentions': {'parse': []}}, files)
        else:
            body = json.dumps(payload).encode() if payload is not None else None
            kind = 'application/json'
        token = bot_token()
        headers = {'Authorization': 'Bot ' + token, 'User-Agent': 'DiscordBot (local ai-agents, 1.0)', 'Content-Type': kind}
        req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=55) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                try:
                    error = json.loads(exc.read())
                    if not isinstance(error, dict):
                        error = {}
                except ValueError:
                    error = {}
                except (urllib.error.URLError, OSError, http.client.HTTPException):
                    if method in ('POST', 'PATCH'):
                        raise RuntimeError('delivery uncertain, check Discord before retrying') from None
                    raise RuntimeError('Discord network request failed') from None
                finally:
                    exc.close()
                if exc.code == 413 or error.get('code') == 40005:
                    raise TooLarge('Discord rejected the attachment size') from None
                if exc.code == 429 and attempt < 4:
                    delay = error.get('retry_after', 5)
                    if not isinstance(delay, (int, float)) or not 0 <= delay < float('inf'):
                        raise RuntimeError('Discord returned HTTP 429 with invalid retry_after') from None
                    time.sleep(delay)
                    continue
                message = str(error.get('message', '')).replace(token, '[redacted]')
                message = re.sub(r'(?i)https://(?:discord\.com|discordapp\.com)/api/(?:v[0-9]+/)?webhooks/[^\s"\']+', '[redacted]', message)
                message = re.sub(r'(?i)authorization\b[^\r\n]*', '[redacted]', message)
                code = error.get('code')
                detail = f' code {code}' if isinstance(code, int) else ''
                raise RuntimeError(f'Discord returned HTTP {exc.code}{detail}: {message}') from None
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                if method in ('POST', 'PATCH'):
                    raise RuntimeError('delivery uncertain, check Discord before retrying') from None
                raise RuntimeError('Discord network request failed') from None

    def get_channel(self, channel_id):
        return self.request('GET', '/channels/' + snowflake(channel_id))

    def list_guild_channels(self):
        return self.request('GET', f'/guilds/{self.guild_id}/channels')

    def create_text_channel(self, name, parent_id):
        return self.request('POST', f'/guilds/{self.guild_id}/channels', {'name': name, 'type': 0, 'parent_id': snowflake(parent_id)})

    def send_message(self, channel_id, content, files=(), reply_to=None):
        check_content(content)
        payload = {'content': content}
        if reply_to is not None:
            payload['message_reference'] = {'message_id': snowflake(reply_to)}
        return self.request('POST', f'/channels/{snowflake(channel_id)}/messages', payload, files)

    def edit_message(self, channel_id, message_id, content):
        check_content(content)
        return self.request('PATCH', f'/channels/{snowflake(channel_id)}/messages/{snowflake(message_id)}', {'content': content})

    def start_thread_from_message(self, channel_id, message_id, name, auto_archive=10080):
        return self.request('POST', f'/channels/{snowflake(channel_id)}/messages/{snowflake(message_id)}/threads', {'name': name, 'auto_archive_duration': auto_archive})

    def get_messages(self, channel_id, limit=100, before=None, after=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Message limit must be between 1 and 100')
        query = {'limit': limit}
        if before is not None:
            query['before'] = snowflake(before)
        if after is not None:
            query['after'] = snowflake(after)
        return self.request('GET', f'/channels/{snowflake(channel_id)}/messages?' + urllib.parse.urlencode(query))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'doctor'])
    args = parser.parse_args()
    if args.action == 'setup':
        value = validate_token(getpass.getpass('Discord bot token (hidden): ').strip())
        SECRET.write_text(crypt(value.encode('utf-16-le'), True).hex(), encoding='utf-8')
        print('Saved encrypted bot credential for this Windows account.')
        return
    from agent_state import load_config
    config = load_config()
    client = BotClient(config['guild_id'])
    items = [('bot', '/users/@me'), ('guild', '/guilds/' + config['guild_id'])]
    ids = dict.fromkeys([config['agents_category_id'], *config['read_channel_ids']])
    items += [('channel', '/channels/' + channel_id) for channel_id in ids]
    failed = False
    for label, path in items:
        try:
            result = client.request('GET', path)
            print(f'OK {label}: {result.get("username", result.get("name", ""))} ({result["id"]})')
        except RuntimeError as exc:
            failed = True
            print(f'FAIL {label} {path}: {exc}')
    if failed:
        raise SystemExit(1)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc))
        raise SystemExit(1)
