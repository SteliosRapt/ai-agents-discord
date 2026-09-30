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

### Tested
- **Offline:** `py test_toolkit.py`, `py test_agents.py`.
- **Live, 2026-09-30:** one end-to-end run through the stdio MCP server against the real server:
  - channel, thread, status edits, attachment, question
  - owner answer detected in the thread
  - read, list, final summary
- **Not yet tested live:**
  - `send_video` transcoding
  - installation through the Claude/Codex plugin managers, including whether Codex resolves `py`, the environment allowlist in `codex.mcp.json`, and DPAPI under Codex

## Webhook quick start (PowerShell)

```powershell
Set-Location "$HOME\Desktop\Discord_Agent_Toolkit"
python discord_client.py send "Task finished"
python discord_client.py send "Here is the report" --file "C:\path\report.pdf"
python send_video.py "C:\path\movie.mp4" --message "Animation finished"
python run_and_notify.py --name "My job" -- python "C:\path\job.py"
```

Python 3.10+ is required. Basic messages and attachments use only the standard library. Video compression additionally needs FFmpeg (with libx264) and ffprobe on PATH; alternatively pass `--ffmpeg C:\path\ffmpeg.exe --ffprobe C:\path\ffprobe.exe`. Get FFmpeg through https://ffmpeg.org/download.html. Compression uses the source video's measured duration, preserves audio if present, and writes separate copies beside the source. No images or previews are generated for review.

To configure another webhook, run `python discord_client.py setup`. Paste its URL at the hidden prompt. Create webhooks in Discord server settings → Integrations → Webhooks, choose the intended channel, and copy the URL. Webhooks post to their configured destination; channel names in scripts do not redirect them.

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

Run `python test_toolkit.py` for offline tests of multipart construction, rate-limit retry, oversized upload handling, credential encryption, and conditional video tiers. These tests do not post to Discord. The underlying webhook and DPAPI technique successfully delivered a live test in the original setup. Generic video transcoding requires FFmpeg and has not been exercised on a real movie as part of creating this toolkit; no original render was inspected or changed.
