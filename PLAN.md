# AI Agents Discord plugin — evolution plan (proposal, 2026-09-30)

Status (2026-10-01): **phases 1–5 implemented, Codex-verified, merged**. See README "Tested" for what ran live.

| Phase | Delivered |
|---|---|
| 1 | Bot-backed MCP server (9 tools), Claude + Codex manifests, discord skill, live E2E test |
| 2 | Hook-fed session registry, file inbox, stdlib Gateway client, owner-only `/agents` + `/send` listener, autostart commands |
| 3 | Reliable inbox delivery, zero-token alerts for thread-linked sessions (attention / long turn, owner-only ping), live `run_and_notify` status |
| 4 | Independent Codex security + reliability review: 17 findings fixed, plus 3 races found while verifying |
| 5 | No-posting live smoke test (`smoke_test.py`), waiting/working/idle state in `/agents` |

Deferred by owner decision: remote task inbox / remote control. Not done (needs owner or low value): per-agent webhook identities (needs Manage Webhooks), message search, forum/digest, moving plugin files into a subfolder so the Codex cache copy excludes `.git`/`.orc`. Sources: Sol second opinion (ask `0930-202129-053`) and Sol web research (full cited report: `.orc/asks/0930-202129-020.final.md`).

## Answers to the direct questions

| Question | Today (webhook) | With a Discord bot |
|---|---|---|
| Fetch data from a channel? | No. A webhook can only read back its own messages by ID. | Yes. `GET /channels/{id}/messages` (100 per call, cursor paging). Needs the **Message Content** privileged intent toggled in the Developer Portal (free, no review under 100 servers) plus View Channel + Read Message History. |
| Model picks the channel? | No. A webhook is bound to one channel (it can target threads/forum posts inside it). | Yes, restricted in code to an allowlist of numeric channel IDs. |
| Create channels? | No. | Yes with `MANAGE_CHANNELS`. Caveat: Discord-side that permission also allows deletion/editing, so scope it with a role whose permission only applies to the "AI Agents" category. Threads need only `CREATE_PUBLIC_THREADS` and are the better unit for per-session work. |

Conclusion: add a **bot** (you create it in the Developer Portal; I never handle the token in chat). The webhook stays as the zero-dependency CLI notifier — an explicit separate path, never a silent fallback.

## Security model (read this first)

- **DPAPI is not a boundary against agents.** Any agent running as your Windows account can decrypt the token and call Discord directly, bypassing the plugin's allowlist. The allowlist protects against model mistakes and prompt injection steering tool calls; the *real* boundary is the bot's Discord permissions. So: no Administrator, a dedicated role, permissions granted only on the AI Agents category.
- Destinations and upload sources enforced by numeric ID / canonical path roots in `config.toml` (not by model-supplied names). Prevents file exfiltration (`send_message(files=["~/.ssh/id_rsa"])`).
- Content read from Discord is returned to the model wrapped as untrusted data with author ID; only messages from your owner user ID count as answers/approvals.
- No delete / ban / role / webhook-token tools. Mentions stay disabled.
- Remote control (Discord → agent does things) only through bounded, owner-checked requests; policy changes never come from chat.
- Failures fail loudly (no transport switching, no automatic re-posting on uncertain network errors — existing behaviour kept).

## Architecture

- **One Python MCP server over stdio** (`ai_agents_mcp.py`), official `mcp` SDK (FastMCP), pinned in `requirements.txt`, launched with `py`. Reuses `discord_client.py` logic (multipart, 429 handling, size rejection) generalised to bot auth.
- **Shared state in SQLite** (`state.db`): project→channel/thread map, job receipts (dedupe), open questions, task claims with leases. Lets several agents run concurrently without fighting over threads.
- **Optional later: one local broker daemon** holding the only Gateway connection (for buttons / real-time push). Not needed for phases 1–3.
- **Packaging (one folder, two manifests)**, per current docs and a known Codex loader bug (#47925: portable root `plugin.json` skips hooks → don't ship one):

```text
Discord_Agent_Toolkit/
  .claude-plugin/marketplace.json        # Claude local marketplace
  .agents/plugins/marketplace.json       # Codex local marketplace
  plugin/
    .claude-plugin/plugin.json
    .codex-plugin/plugin.json
    .mcp.json                            # py ai_agents_mcp.py
    skills/discord/SKILL.md              # when/how to notify, ask, read
    hooks/claude.json  hooks/codex.json
    scripts/                             # hook adapters, CLI, send_video
```

Install: `/plugin marketplace add <folder>` + `/plugin install ai-agents@...` in Claude; `codex plugin marketplace add <folder>` + `codex plugin add ai-agents@...` in Codex.

## Proposed features (ranked value/effort)

### Phase 1 — plugin + bot core
1. **Dual-host plugin** (above) + `discord` skill. *Value high / effort low.*
2. **Bot setup**: `py ai_agents.py setup-bot` stores token with DPAPI (hidden prompt); `config.toml` holds guild ID, owner user ID, allowed channel IDs, default channel, agent category ID, upload roots. `py ai_agents.py doctor` checks token, intent, and permissions per channel.
3. **MCP tools**: `list_channels`, `send_message(channel, content, files[], thread_id?, reply_to?)`, `read_messages(channel, limit≤100, before/after)`, `create_thread`, `create_channel` (category-locked, name-prefixed), `edit_message`, `add_reaction`, `send_video` (existing conditional compression, originals preserved). *High / medium.*
4. Rename sender identity from "NotifAIcations" to "AI Agents"; update README.

### Phase 2 — automatic notifications
5. **Hooks**: Claude `Notification` (needs permission / idle) → ping your thread; `Stop` only when the turn ran longer than N minutes (otherwise it's spam). Codex `Stop` hook via `.codex-plugin`. Post a short summary, never the full hook payload (it contains prompts/project content). *High / low.*
6. **Live job status**: `run_and_notify` posts one message and edits it in place (elapsed, last log line), then final status + log tail attached on failure; renders get the output video via `send_video`. Job IDs + receipts in SQLite for duplicate prevention. *High / medium.*
7. **Per-project routing**: working directory → a channel (or forum) under AI Agents, auto-created once; each agent session → its own thread. Archived threads are reopened, not duplicated. *High / medium.*

### Phase 3 — two-way
8. **Ask the human**: `post_question(text, options?)` → `question_id`; `check_answer(question_id, wait_s≤50)` (Codex MCP tool timeout is 60 s, so no long blocking). Answers = your reply-to message, or your reaction (verified via the reaction-users endpoint), or a Discord poll. Expiry → explicit "no answer". *High / medium.*
9. **Attachments in**: `download_attachment` so you can drop a reference image/.blend/CSV from your phone for an agent. Restricted to a downloads folder. *Medium / low.*
10. **Search**: `search_messages` via guild message search for research runs. *Medium / low.*
11. **Remote inbox**: `#inbox` channel; you post tasks from your phone; agents call `claim_task` (SQLite lease, one agent per task) and report back in a thread. *High / high.*

### Phase 4 — optional / evaluate
12. **Gateway broker + buttons**: Allow/Deny buttons for tool approvals, real-time push. Claude: evaluate the **official Channels Discord plugin** (`discord@claude-plugins-official`) instead of building — it already does push-into-session and permission relay with owner pairing, but stores its token in a plaintext `.env` (conflicts with your DPAPI rule). Codex: a bridge on `codex app-server`. *High / high, highest risk.*
13. **Per-agent identities** (Claude/Codex/project name + avatar) via one webhook per channel with username override. Needs `MANAGE_WEBHOOKS`; cosmetic. *Low / low.*
14. Forum "runs" log, daily digest. *Low / medium.*

Not proposed: delete/moderation/role tools, Administrator permission, Discord user-account automation (against Discord ToS).

## Suggested server layout ("AI Agents")

```text
AI Agents
  #general         existing webhook notifications
  #inbox           tasks you queue from your phone (phase 3)
  AGENTS (category, the only place the bot can create channels)
    #blender-renders   thread per session/job
    #<project>         auto-created per project
```

## Decisions (owner, 2026-09-30)

1. **Bot: yes, owner-only.** You create it (Public Bot OFF so nobody else can invite it). Every inbound path checks `owner_user_id`.
2. **Threads** (they are sub-channels: nested under a channel in the sidebar, archive when idle, reopenable). One text channel per project under the AGENTS category; one thread per agent session.
3. **No remote control / task inbox for now.** Main purpose: notify, plus task-level milestones while an agent works, plus a Discord command to list running Claude Code/Codex sessions and send one of them a file from your phone.
4. **Build our own, the same for Claude and Codex** (not the official Claude Channels plugin).
- Usage restraint: agents declare *before starting* when they'll post (`start_session(plan, max_messages)`), and the server enforces that budget. Status edits don't ping the phone.

## Phase 2 design: `/agents`, targeted files, explicit tagging

**Who is running (as built).** A command hook (`session_hook.py`, declared in `hooks/claude-hooks.json` and `hooks/codex-hooks.json`) runs on SessionStart, UserPromptSubmit, PostToolUse and SessionEnd in every Claude Code/Codex session. It records the host's own session id, working folder and host process id in `~/.ai-agents/state.db` (`host_sessions`), with a 4-character handle. No model call is involved; the cost is about 160 ms per event. When an agent's `start_session` tool call completes, the same hook links that session to its Discord thread and title. Liveness = not ended, recent activity, and host process alive. The earlier idea of matching MCP processes by parent pid was dropped: hooks see the real session id, and a Codex app process can host several threads.

**The Discord command.** A small always-on local listener (`ai_agents_daemon.py`, hidden window, started at login by Task Scheduler after you approve that) holds the only Gateway connection. It registers owner-only guild slash commands:
- `/agents`: lists live sessions with host · project · task title · thread link · last activity, visible only to you.
- `/send agent:<autocomplete> file:<attachment> note:<text>`: downloads the attachment into `inbox/<session>/` and reacts ✅ with the saved path.

**How the agent gets it (no interruption, no wrong agent).** Nothing is pushed into a session. `/send` saves the file to `~/.ai-agents/inbox/<handle>/` plus an inbox row. The targeted session's own PostToolUse/UserPromptSubmit hook takes the pending rows atomically and injects them as `additionalContext` at that session's **next tool step or prompt**. Other sessions never see them; idle sessions don't wake up. The daemon's confirmation says the agent sees it at its next step. If the session has a thread, a visible note is also posted there.

**Explicit tagging rules** (agents only react to):
1. A **Discord Reply** to the agent's own `ask_owner` question, inside its thread. This is the answer path, and works today via `check_answer`.
2. `/send` (or @-mentioning the bot inside that agent's thread) with a file/note, delivered as above.

Everything else, including plain messages in threads, other channels, and anything not from you, is ignored. Agents never read Discord on their own initiative.

**Why a listener is needed:** slash commands and mentions are events. Without a Gateway connection (or a public HTTPS endpoint, which we avoid), nothing on the PC can react to `/agents`. The listener is a normal local script, not an AI wakeup, and it has no model access.

## Execution plan once approved

- Phase 1: one Codex implementer (coupled files: server, client refactor, config, tests), risk **high** (credentials + outbound file upload), checks = `py test_toolkit.py` + new offline tests with a mocked Discord API. I write the manifests/skill and review the diff myself.
- Live verification: one real send/read/thread-create in an allowlisted channel after you approve that destination — reported separately from phone-notification delivery.
