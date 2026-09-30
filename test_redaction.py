import os
import unittest
from unittest.mock import patch
from redaction import redact, MASK

# Synthetic credential-shaped values only.
TOKEN = 'MTU1NDkzMzQwODI4NTU5Nzc4Nw.Gabc12.' + 'x' * 38
HOOK = 'https://discord.com/api/webhooks/1554904867439509625/' + 'y' * 68

class Tests(unittest.TestCase):
    def test_token_shapes_and_urls(self):
        for text in (f'token={TOKEN}', f'see {HOOK} now', HOOK.replace('discord.com', 'canary.discordapp.com').replace('/api/', '/api/v10/')):
            out = redact(text)
            self.assertNotIn('x' * 38, out); self.assertNotIn('y' * 68, out); self.assertIn(MASK, out)

    def test_authorization_header_keeps_label(self):
        self.assertEqual(redact('Authorization: Bot abc.def.ghi\nnext'), f'Authorization: {MASK}\nnext')

    def test_env_and_extra_values(self):
        with patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'plainsecretvalue'}):
            self.assertEqual(redact('a plainsecretvalue b other', extra=['other']), f'a {MASK} b {MASK}')

    def test_ordinary_text_untouched(self):
        text = 'Render 120/480 frames, ETA 25 min · C:\\Users\\user\\Desktop\\scene.blend · Ελληνικά ✅'
        self.assertEqual(redact(text), text)

if __name__ == '__main__': unittest.main()
