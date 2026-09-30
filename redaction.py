"""Mask Discord credentials in text before it is logged or posted. Standard library only."""
import os
import re

MASK = '[redacted]'
PATTERNS = [
    # Webhook URLs carry their token in the path.
    re.compile(r'https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w.-]+', re.I),
    # Bot tokens: base64(user id) . timestamp . HMAC.
    re.compile(r'\b[MNO][\w-]{23,27}\.[\w-]{6,7}\.[\w-]{27,}'),
    # Authorization headers, whatever the scheme.
    re.compile(r'(?im)(authorization\s*[:=]\s*)[^\r\n]+'),
]
ENV_SECRETS = ('DISCORD_BOT_TOKEN', 'DISCORD_WEBHOOK_URL')

def redact(text, extra=()):
    """Return text with known secret values and credential-shaped strings masked."""
    text = str(text)
    for value in (*extra, *(os.environ.get(name) for name in ENV_SECRETS)):
        if value:
            text = text.replace(value, MASK)
    for pattern in PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if pattern.groups else '') + MASK, text)
    return text
