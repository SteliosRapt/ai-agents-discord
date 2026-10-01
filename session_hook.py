"""Register host activity and deliver targeted file inbox items without model calls."""
import argparse
import json
from pathlib import Path
import re
import sys
import time
import agent_state
import redaction

def post_alert(host, row, payload):
    """Only linked, eligible hook events load config or the bot client."""
    event = payload['hook_event_name']
    if not row['thread_id']:
        return 0
    if host == 'claude' and event == 'Notification':
        # Idle reminders fire ~60 s after every turn; the Stop alert already covers long turns.
        if payload.get('notification_type') == 'idle_prompt' or 'waiting for your input' in (payload.get('message') or '').lower():
            return 0
        text = '⏳ Needs your attention: ' + redaction.redact(payload.get('message') or '')[:300]
    elif host == 'codex' and event == 'PermissionRequest':
        text = '⏳ Codex is waiting for approval: ' + payload.get('tool_name', '')
    elif event == 'Stop' and row['last_prompt_at'] is not None:
        text = None
    else:
        return 0
    stage = 'load alert config'
    try:
        config = agent_state.load_config()
        if text is None:
            minutes = (row['last_seen'] - row['last_prompt_at']) / 60
            if minutes < config['long_turn_minutes']:
                return 0
            text = f'✅ Turn finished after {int(minutes)} min'
            message = payload.get('last_assistant_message')
            if message:
                text += ': ' + redaction.redact(message)[:300]
        users = (config['owner_user_id'],) if config['alert_mentions_owner'] else ()
        if users:
            text = f'<@{users[0]}> ' + text
        stage = 'post Discord alert'
        from bot_client import BotClient
        BotClient(config['guild_id']).send_message(row['thread_id'], text, mention_users=users)
        return 0
    except Exception as exc:
        # Exception text may contain credentials from an HTTP/config failure. Never echo it.
        print(f'AI Agents could not {stage} ({type(exc).__name__}); check config.toml and bot setup; '
              'check Discord before retrying because delivery may be uncertain.', file=sys.stderr)
        return 1

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=['claude', 'codex'], required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.buffer.read().decode('utf-8', errors='strict'))
    except (ValueError, UnicodeError):
        print('Invalid hook input: expected one JSON object', file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print('Invalid hook input: expected one JSON object', file=sys.stderr)
        return 1
    for key in ('session_id', 'hook_event_name', 'cwd'):
        if key not in payload or not isinstance(payload[key], str) or not payload[key]:
            print('Missing or invalid required hook field: ' + key, file=sys.stderr)
            return 1
    session_id, event = payload['session_id'], payload['hook_event_name']
    event_at = time.time()
    agent_state.ROOT.mkdir(parents=True, exist_ok=True)
    state = agent_state.State()
    inbox_output = False
    try:
        # Subagents report tool events under the parent's session id (Claude and Codex) and mark them with agent_id.
        # Their steps prove nothing about the main model having seen an injection, and their context is not the
        # owner's target, so they neither confirm nor receive inbox items.
        main_agent = not payload.get('agent_id')
        if main_agent:
            state.confirm_inbox(session_id, event, event_at)
        row = state.touch_host_session(args.host, session_id, payload['cwd'], agent_state.host_process_pid(), event)
        tool = payload.get('tool_name', '')
        if event == 'PostToolUse' and 'start_session' in tool and ('ai-agents' in tool or 'ai_agents' in tool):
            response = json.dumps(payload.get('tool_response'))
            mcp = re.search(r'\\*"session_id\\*"\s*:\s*\\*"([0-9a-fA-F]{32})\\*"', response)
            thread = re.search(r'\\*"thread_id\\*"\s*:\s*(?:\\*")?([0-9]+)(?:\\*"|(?=\s*[,}]))', response)
            if mcp and thread:
                state.link_host_session(session_id, mcp.group(1), thread.group(1), (payload.get('tool_input') or {}).get('title'))
        if main_agent and event in ('PostToolUse', 'UserPromptSubmit'):
            rows = state.take_inbox(session_id, retry_before=event_at, event=event)
            if rows:
                text = 'AI Agents inbox: the owner sent you these via Discord /send\n'
                text += '\n'.join(f'{Path(item["path"]).resolve()}: {item["note"] or ""}'
                                  + (' (re-sent)' if item['delivered_at'] is not None else '') for item in rows)
                print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}), flush=True)
                inbox_output = True
        return post_alert(args.host, row, payload)
    finally:
        # Keep inbox claims active until context output and all remaining hook duties have finished.
        state.close(inbox_delivered=inbox_output)

if __name__ == '__main__':
    sys.exit(main())
