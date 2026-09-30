"""Register host activity and deliver targeted file inbox items without model calls."""
import argparse
import json
from pathlib import Path
import re
import sys
import agent_state

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=['claude', 'codex'], required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
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
    agent_state.ROOT.mkdir(parents=True, exist_ok=True)
    state = agent_state.State()
    try:
        state.touch_host_session(args.host, session_id, payload['cwd'], agent_state.host_process_pid(), event)
        tool = payload.get('tool_name', '')
        if event == 'PostToolUse' and 'start_session' in tool and ('ai-agents' in tool or 'ai_agents' in tool):
            response = json.dumps(payload.get('tool_response'))
            mcp = re.search(r'\\*"session_id\\*"\s*:\s*\\*"([0-9a-fA-F]{32})\\*"', response)
            thread = re.search(r'\\*"thread_id\\*"\s*:\s*(?:\\*")?([0-9]+)(?:\\*"|(?=\s*[,}]))', response)
            if mcp and thread:
                state.link_host_session(session_id, mcp.group(1), thread.group(1), (payload.get('tool_input') or {}).get('title'))
        if event in ('PostToolUse', 'UserPromptSubmit'):
            rows = state.take_inbox(session_id)
            if rows:
                text = 'AI Agents inbox: the owner sent you these via Discord /send\n'
                text += '\n'.join(f'{Path(row["path"]).resolve()}: {row["note"] or ""}' for row in rows)
                print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}))
        return 0
    finally:
        state.close()

if __name__ == '__main__':
    sys.exit(main())
