# Instructions for AI agents

Read README.md before using this toolkit. It is independent of the Blender project. The bot-backed plugin is `ai_agents_mcp.py` (MCP tools). Use `discord_client.py` for webhook messages/files, `send_video.py` for conditional compression, and `run_and_notify.py` for a new command's completion notification.

Only send material to Discord when the user has authorized that destination and content. Existing authorization persists; do not demand redundant approval. Do not send a test just because you are reading this folder.

Never print or copy the decrypted webhook URL into a response, log, source file, or command argument. Use the encrypted credential or a private environment variable. Do not read Discord account tokens. The included webhook routes to AI Agents / #general, not arbitrary channels. The bot token (`bot_token.dpapi`) gets the same treatment as the webhook URL: never print, copy or pass it as an argument. Plugin agents follow `skills/discord/SKILL.md`.

Use normal local scripts for ongoing monitoring, not repeated AI wakeups. Run under the account that configured DPAPI and with authorized network access. Don't confuse sandbox/network restrictions with invalid credentials. Use hidden windows for background helpers on Windows; normalize inherited environment names if duplicate Path/PATH entries cause process-launch problems.

Distinguish confirmed API delivery from phone notification delivery. On uncertain network failure, check for duplicates before reposting. Preserve full-quality originals. Don't upload unrelated files or inspect video contents unless the task authorizes it. The `ffprobe` duration read used for encoding is metadata access.

Do not claim transcoding or live delivery was tested unless actually tested. File limits are dated observations, not permanent constants; consult the official links in README.md and handle API size rejection.
