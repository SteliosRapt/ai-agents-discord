"""Real FFmpeg tests using generated fixtures; the sender never accesses Discord."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from discord_client import TooLarge
from send_video import deliver_video


class VideoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which('ffmpeg')
        cls.ffprobe = shutil.which('ffprobe')
        if not cls.ffmpeg or not cls.ffprobe:
            reason = 'Synthetic video tests require FFmpeg and ffprobe on PATH'
            if os.environ.get('AI_AGENTS_REQUIRE_FFMPEG') == '1':
                raise RuntimeError(reason)
            raise unittest.SkipTest(reason)

    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ai-agents-video-test-')))

    def fixture(self, audio):
        source = self.folder / ('with_audio.mkv' if audio else 'silent.mkv')
        command = [self.ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi',
                   '-i', 'testsrc2=size=640x360:rate=12']
        if audio:
            command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
        command += ['-t', '2', '-c:v', 'ffv1']
        if audio:
            command += ['-c:a', 'pcm_s16le']
        subprocess.run([*command, str(source)], check=True, capture_output=True, timeout=30)
        return source

    def assert_playable_copy(self, path, audio):
        result = subprocess.run([self.ffprobe, '-v', 'error', '-show_streams', '-show_format',
                                 '-of', 'json', str(path)], check=True, capture_output=True, timeout=15)
        metadata = json.loads(result.stdout)
        videos = [stream for stream in metadata['streams'] if stream['codec_type'] == 'video']
        audios = [stream for stream in metadata['streams'] if stream['codec_type'] == 'audio']
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]['codec_name'], 'h264')
        self.assertEqual((videos[0]['width'], videos[0]['height']), (640, 360))
        self.assertEqual(len(audios), int(audio))
        if audio:
            self.assertEqual(audios[0]['codec_name'], 'aac')
        self.assertAlmostEqual(float(metadata['format']['duration']), 2, delta=0.2)
        subprocess.run([self.ffmpeg, '-v', 'error', '-xerror', '-nostdin', '-i', str(path),
                        '-f', 'null', '-'], check=True, capture_output=True, timeout=30)

    def test_local_size_budget_encodes_playable_audio_and_silent_copies(self):
        for audio in (False, True):
            with self.subTest(audio=audio):
                source = self.fixture(audio)
                original = hashlib.sha256(source.read_bytes()).digest()
                limit = 0.25
                self.assertGreater(source.stat().st_size, limit * 1024**2)
                accepted = []

                def sender(message, path, limit_mib):
                    if path.stat().st_size > limit_mib * 1024**2:
                        raise TooLarge('Mock local size rejection')
                    accepted.append(path)
                    return {'id': 'offline-video'}

                result = deliver_video(source, 'Generated fixture', sender, limit,
                                       self.ffmpeg, self.ffprobe)
                self.assertEqual(result, {'id': 'offline-video'})
                self.assertEqual(len(accepted), 1)
                self.assertNotEqual(accepted[0], source)
                self.assertLessEqual(accepted[0].stat().st_size, limit * 0.85 * 1024**2)
                self.assert_playable_copy(accepted[0], audio)
                self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), original)

    def test_remote_size_rejection_advances_to_another_real_encoded_tier(self):
        source = self.fixture(False)
        original = source.read_bytes()
        attempts = []

        def sender(message, path, limit_mib):
            attempts.append((message, path))
            if len(attempts) < 3:
                raise TooLarge('Mock Discord size rejection')
            return {'id': 'offline-video'}

        self.assertEqual(deliver_video(source, 'Generated fixture', sender, 20,
                                       self.ffmpeg, self.ffprobe), {'id': 'offline-video'})
        self.assertEqual(attempts[0][1], source)
        self.assertIn('720p', attempts[1][0])
        self.assertIn('480p', attempts[2][0])
        self.assertNotEqual(attempts[1][1], attempts[2][1])
        for _, path in attempts[1:]:
            self.assert_playable_copy(path, False)
        self.assertEqual(source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
