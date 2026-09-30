# AI Agents Discord toolkit and plugin

Two independent paths into the **AI Agents** Discord server:

- **Plugin (bot):** a local MCP server (`ai_agents_mcp.py`) that Claude Code and Codex sessions use to post declared milestones, files and videos into their own thread, ask the owner a question, and read allowed channels.
- **Webhook scripts:** standard-library scripts that post to #general (`discord_client.py`, `send_video.py`, `run_and_notify.py`). They need no bot and no model; use them from your own scripts.

Neither path falls back to the other.

## Plugin (Claude Code + Codex)

### One-time setup
1. **Discord:**
   - Create a private bot (Public Bot off, Install Link = None) with the **Message Content** intent.
   - Invite it with `bot` + `applications.commands` and View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Read Message History, Attach Files, Embed Links and Add Reactions.
   - Grant its role **Manage Channels on the AGENTS category only**.
2. **Config:** copy `config.example.toml` to `%USERPROFILE%\.ai-agents\config.toml` and fill in the server, owner, AGENTS-category and read-channel IDs plus `upload_roots`. All runtime data (config, tokens, `state.db`) lives in that folder, not in the plugin folder: Claude and Codex each install their own copy of the plugin, but share one config, one token and one session registry.
3. **Token:** run `py bot_client.py setup` and paste the token at the hidden prompt. It is stored DPAPI-encrypted in `%USERPROFILE%\.ai-agents\bot_token.dpapi` and only decrypts under this Windows account. `DISCORD_BOT_TOKEN` in the environment overrides it.
4. **Check:** run `py bot_client.py doctor`. It makes read-only requests and never prints the token.
5. **Python:** `mcp` (see `requirements.txt`) must be importable by `py` for the account that runs Claude Code/Codex.

### Install
Both manifests live in this folder: `.claude-plugin/` and `claude.mcp.json` for Claude (deliberately not `.mcp.json`, which Claude would also load as a broken project server when working in this folder); `.codex-plugin/`, `codex.mcp.json` and `.agents/plugins/marketplace.json` for Codex. The skill in `skills/discord/` is shared.

```text
claude plugin marketplace add C:\Users\user\Desktop\Discord_Agent_Toolkit
claude plugin install ai-agents@ai-agents-local
codex plugin marketplace add C:\Users\user\Desktop\Discord_Agent_Toolkit
codex plugin add ai-agents@ai-agents-local
```

**How each app runs it (observed 2026-09-30):**
- **Claude Code** runs a local-folder plugin **in place** from this folder. Code changes apply when a session restarts.
- **Codex** runs a **copy** from `~/.codex/plugins/cache/ai-agents-local/ai-agents/<version>`. After changing code, bump the version in `.codex-plugin/plugin.json` and re-run `codex plugin add ai-agents@ai-agents-local`. That update step hasn't been exercised yet.
- Neither copy holds credentials; both read `%USERPROFILE%\.ai-agents`.

Restart the app afterwards. Each session then starts its own `ai_agents_mcp.py --host claude|codex` process. That process registers itself in `state.db` without any model call; list live sessions with `py agent_state.py sessions`.

### How agents use it
Behaviour is guided by `skills/discord/SKILL.md` rather than enforced quotas:
- An agent uses Discord only for long-running work or when asked.
- It tells you in chat when it will post, then calls `start_session(title, plan, project_dir)`.
- That creates a channel for the project under AGENTS (once per project folder) and a thread for the session. The plan is posted first.

| Tool | What it does |
|---|---|
| `update_status` | Edits one status message in place (no phone ping) |
| `post_milestone` | Text plus up to 10 files |
| `send_video` | Conditional compression; the original is kept |
| `ask_owner` / `check_answer` | A question and its answer |
| `read_messages`, `list_channels` | Reading |
| `end_session` | Final summary |

**Answering a question:** post anything in that agent's thread after the question. Only your user ID counts, only in that thread, and only until the agent's next question. Answers reach the agent as untrusted data.

**Safety:**
- Uploads must resolve inside `upload_roots`. Files in the plugin folder, in `~/.ai-agents`, and any `*.dpapi` file are always refused.
- Agents can read only their own thread, their project channel and `read_channel_ids`.
- There are no delete, moderation or role tools, and mentions are disabled.
- Caveat: DPAPI protects the token from other Windows accounts, not from code running as you. The bot's Discord permissions are the real boundary.

### Listener: `/agents` and `/send` from your phone
`ai_agents_daemon.py` is a small local listener. It holds one Discord Gateway connection, uses a standard-library WebSocket client (no extra packages) and has no model access.

```text
py ai_agents_daemon.py run                    # foreground; logs to %USERPROFILE%\.ai-agents\daemon.log
py ai_agents_daemon.py status                 # running?, autostart task, last log lines
py ai_agents_daemon.py install-autostart      # Task Scheduler: start hidden (pythonw) at logon
py ai_agents_daemon.py uninstall-autostart
```

- **`/agents`:** lists running Claude Code/Codex sessions. Each line shows a 4-character handle, the app, the project folder, the Discord task title and thread (if any), and the last activity. Only you see it.
- **`/send agent:<handle> file:<attachment> note:<text>`:**
  - The handle autocompletes.
  - The file is saved to `%USERPROFILE%\.ai-agents\inbox\<handle>\`, and the session's thread (if any) gets a visible note.
  - **Only that session** sees it, at its next tool call or prompt, as injected context. Idle sessions are not woken up.
- **Access:** both commands are owner-only (checked by user ID and server ID) and hidden from non-admins.

**Where the session list comes from:**
- `session_hook.py` runs as a plugin hook (`hooks/claude-hooks.json`, `hooks/codex-hooks.json`) on SessionStart, UserPromptSubmit, PostToolUse and SessionEnd.
- It records each session in `state.db` and delivers inbox items. It makes no model calls and costs about 160 ms per event.
- Sessions appear after they start with the hooks installed; sessions already open need a restart.
- **Codex skips plugin hooks until you trust them once in Codex's `/hooks` review.** Until then Codex sessions don't appear in `/agents` and can't receive `/send`, although their MCP tools work.

### Automatic alerts (no model tokens)
Alerts only fire for sessions whose agent opened a Discord thread with `start_session`. They're posted into that thread by the hook:
- **Claude asks for a permission, or Codex waits for an approval:** "⏳ Needs your attention …"
- **A turn ran at least `long_turn_minutes` (default 10):** "✅ Turn finished after N min: <last reply, 300 chars>"
- Claude's "waiting for your input" idle reminders are ignored, since the turn-finished alert covers them.
- With `alert_mentions_owner = true` (the default), alerts ping **only you** (`<@you>`, never roles or @everyone), so the phone notifies even for threads you don't follow.

Both keys are optional in `config.toml`.

### Live job status (webhook)
`py run_and_notify.py --name "Render shot 12" --every 60 -- blender -b scene.blend -a`
- Posts "▶ started", then edits that one message every `--every` seconds (at least 15) with the elapsed time and last output line. Edits don't buzz the phone.
- At the end it edits the message and posts a new ✅/❌ message, which does notify. A failure attaches the last 60 log lines.
- The full log goes to `%USERPROFILE%\.ai-agents\jobs\`, or to `--log`. The script exits with the command's exit code.

### Tested
- **Offline:** `py test_toolkit.py`, `py test_agents.py`.
- **Live, 2026-09-30:** one end-to-end run through the stdio MCP server against the real server:
  - channel, thread, status edits, attachment, question
  - owner answer detected in the thread
  - read, list, final summary
- **Installed, 2026-09-30:**
  - Claude: the plugin server connected in all open sessions.
  - Codex: a `codex exec` session started the plugin copy, decrypted the token from `~/.ai-agents` and called `list_channels` successfully.
- **Phase 2, live 2026-10-01:**
  - The listener connected to the real Gateway (READY) and registered `/agents` and `/send`.
  - A fresh Claude session ran the hooks: SessionStart registered it, UserPromptSubmit took the queued inbox item, SessionEnd closed it.
  - Codex 0.2.0 install and MCP tools work; its hooks await your `/hooks` trust.
- **Phase 4, 2026-10-01:** an independent Codex security + reliability review found 17 defects; all are fixed, each with a regression test.
  - Three further races surfaced while verifying the fixes and were fixed too: the answer window with concurrent questions, and two same-name `/send` download races.
  - Live:
    - the listener reconnected and resumed on its own overnight
    - the hook stores a Greek/emoji working folder correctly
    - Codex 0.4.0 installed
- **Trust assumption:** `%USERPROFILE%\.ai-agents` and its parent folders are not writable by anyone else.
- **Not yet tested live:**
  - `/agents` and `/send` issued from Discord (needs the owner)
  - Phase 3 alerts and `run_and_notify` live status (not posted overnight so your phone stays quiet; offline tests cover payloads and mentions)
  - the model's reply to an injected inbox item (the standalone `claude` CLI login had expired)
  - `send_video` transcoding
  - a full posting session started by a real Claude or Codex agent (as opposed to the scripted MCP client)

## Webhook quick start (PowerShell)

```powershell
Set-Location "$HOME\Desktop\Discord_Agent_Toolkit"
py discord_client.py send "Task finished"
py discord_client.py send "Here is the report" --file "C:\path\report.pdf"
py send_video.py "C:\path\movie.mp4" --message "Animation finished"
py run_and_notify.py --name "My job" -- py "C:\path\job.py"
```

Python 3.10+ is required. Basic messages and attachments use only the standard library. Video compression additionally needs FFmpeg (with libx264) and ffprobe on PATH; alternatively pass `--ffmpeg C:\path\ffmpeg.exe --ffprobe C:\path\ffprobe.exe`. Get FFmpeg through https://ffmpeg.org/download.html. Compression uses the source video's measured duration, preserves audio if present, and writes separate copies beside the source. No images or previews are generated for review.

To configure another webhook, run `py discord_client.py setup`. Paste its URL at the hidden prompt. Create webhooks in Discord server settings → Integrations → Webhooks, choose the intended channel, and copy the URL. Webhooks post to their configured destination; channel names in scripts do not redirect them.

## Credentials and Windows execution

`%USERPROFILE%\.ai-agents\webhook.dpapi` contains the URL encrypted by Windows DPAPI. It only decrypts under the Windows account/environment that saved it. It was copied from the working notification setup; no project paths are needed to use it. If another account or sandbox cannot decrypt it, run setup in the same environment that will execute the sender. Alternatively supply `DISCORD_WEBHOOK_URL` privately as an environment variable. Do not put a webhook in documentation, command history, commits, or tool output.

The earlier project setup encountered two issues: sandboxed processes lacked network access, and credentials encrypted under the sandbox identity could not be read by the normal Windows process. Running the sender with authorized network access and configuring DPAPI under that same account solved both. This toolkit decrypts directly through Windows APIs, avoiding PowerShell module-loading problems.

## Limits and conditional uploading

Checked September 9, 2026:

- The current official API source states **20 MiB per file** by default; server boost levels can affect limits. https://github.com/discord/discord-api-docs/blob/main/developers/reference.mdx
- Discord's support FAQ says free uploads increased to 20 MB in August 2026. https://support.discord.com/hc/en-us/articles/25444343291031-File-Attachments-FAQ
- An older rendered API documentation page still stated 10 MiB. Treat actual API acceptance as authoritative, and recheck documentation when updating this toolkit. https://docs.discord.com/developers/reference
- A webhook execution supports multipart file attachments with `payload_json` and `files[0]`. This toolkit sends one file per message. https://docs.discord.com/developers/resources/webhook
- Message content is limited here to 2,000 characters. Mentions are disabled, including @everyone.
- HTTP 413 or Discord error code 40005 triggers a smaller video tier. Authentication, permission, network, and rate-limit errors do **not** imply a smaller file is needed.
- HTTP 429 respects Discord's `retry_after`, with a bounded retry count. A network timeout is reported as uncertain delivery rather than blindly reposted. Check Discord before rerunning an uncertain send.

The CLI defaults to a 20 MiB local threshold, configurable with `--limit-mib`. The video sender first tries the original if it fits. On size rejection or excess local size, it tries:

| Maximum resolution | File budget | Audio target |
|---|---:|---:|
| 720p | 9 MiB | 96 kbps |
| 480p | 4.5 MiB | 64 kbps |
| 360p | 2.2 MiB | 48 kbps |
| 240p | 1 MiB | 32 kbps |

Budgets shrink further when `--limit-mib` is smaller. Video bitrate is computed from duration with headroom, and actual output size is checked before each upload. Each Discord message reports the maximum resolution, target bitrates, and measured file size. The original is never overwritten. Very long videos may not fit at usable bitrate; after all tiers are exhausted the command exits with an error instead of looping forever. Compression is conditional on size, not on other upload errors.

## Import from another Python project

```python
import sys
sys.path.insert(0, r'C:\Users\user\Desktop\Discord_Agent_Toolkit')
from discord_client import send
result = send('Analysis complete', r'C:\path\results.csv')
message_id = result['id']
```

Keep a returned message ID in your own job state if you need duplicate prevention across restarts. The standalone client does not maintain global deduplication: intentionally repeated commands send repeated messages. `run_and_notify.py` wraps a new foreground command; it does not attach to already-running jobs and cannot notify if the whole computer loses power or networking.

## Phone notifications

Discord receiving a message does not prove the iPhone displayed a push alert. Enable the channel/server's message notifications and Discord notifications on the phone. Focus settings, muted channels, and desktop activity can affect push delivery.

## Validation

Run `py test_toolkit.py` (and `test_agents`, `test_hooks`, `test_gateway`, `test_daemon`, `test_jobs`) for offline tests of multipart construction, rate-limit retry, oversized upload handling, credential encryption, and conditional video tiers. These tests do not post to Discord. The underlying webhook and DPAPI technique successfully delivered a live test in the original setup. Generic video transcoding requires FFmpeg and has not been exercised on a real movie as part of creating this toolkit; no original render was inspected or changed.
