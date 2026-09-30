"""Send any video, shrinking conditionally with FFmpeg; preserve the original."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from discord_client import send, TooLarge

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('video', type=Path)
    p.add_argument('--message', default='Video ready')
    p.add_argument('--limit-mib', type=float, default=20)
    p.add_argument('--ffmpeg', default='ffmpeg')
    p.add_argument('--ffprobe', default='ffprobe')
    a = p.parse_args()
    source = a.video.resolve()
    try:
        print('Delivered:', send(f'{a.message} | Original file | {source.stat().st_size / 1024**2:.2f} MiB', source, a.limit_mib)['id'])
        return
    except TooLarge:
        pass
    if not shutil.which(a.ffmpeg) or not shutil.which(a.ffprobe):
        raise RuntimeError('Compression needs FFmpeg and ffprobe on PATH, or their explicit paths. Original was preserved.')
    meta = json.loads(subprocess.check_output([a.ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(source)]))
    duration = float(meta['format']['duration'])
    if duration <= 0:
        raise ValueError('Video duration must be positive')
    copies = source.parent / (source.stem + '_discord_copies')
    copies.mkdir(exist_ok=True)
    for height, budget, audio in [(720, 9, 96), (480, 4.5, 64), (360, 2.2, 48), (240, 1, 32)]:
        budget = min(budget, a.limit_mib * .85)
        bitrate = int(budget * 1024**2 * 8 * .90 / duration / 1000 - audio)
        if bitrate < 50:
            continue
        out = copies / f'{source.stem}_{height}p.mp4'
        # Preserve aspect ratio and avoid upscaling. Encoder metadata is not a visual review.
        vf = f'scale=-2:trunc(min(ih,{height})/2)*2'
        with (copies / 'encoding.log').open('ab') as log:
            subprocess.run([a.ffmpeg, '-y', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-vf', vf, '-c:v', 'libx264', '-preset', 'medium', '-b:v', f'{bitrate}k', '-maxrate', f'{bitrate}k', '-bufsize', f'{bitrate*2}k', '-c:a', 'aac', '-b:a', f'{audio}k', '-movflags', '+faststart', str(out)], check=True, stdout=log, stderr=log)
        if not 0 < out.stat().st_size <= budget * 1024**2:
            continue
        description = f'{a.message} | Compressed, up to {height}p; target video {bitrate} kbps / audio {audio} kbps; {out.stat().st_size/1024**2:.2f} MiB. Original preserved.'
        try:
            print('Delivered:', send(description, out, a.limit_mib)['id'])
            return
        except TooLarge:
            continue
    raise RuntimeError('All size budgets exhausted; original preserved. Use an authorized file-sharing service or split the video.')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc))
        raise SystemExit(1)
