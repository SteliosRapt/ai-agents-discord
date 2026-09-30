# NotifAIcations Discord toolkit

Standalone Python scripts for any project. No Blender scene, render folder, Codex API, bot token, or AI polling required. The local encrypted webhook is configured for the existing NotifAIcations / #general destination. This is an incoming webhook: it sends messages and attachments, but cannot read channel history or direct-message users.

## Quick start (PowerShell)

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

`webhook.dpapi` contains the URL encrypted by Windows DPAPI. It only decrypts under the Windows account/environment that saved it. It was copied from the working notification setup; no project paths are needed to use it. If another account or sandbox cannot decrypt it, run setup in the same environment that will execute the sender. Alternatively supply `DISCORD_WEBHOOK_URL` privately as an environment variable. Do not put a webhook in documentation, command history, commits, or tool output.

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
