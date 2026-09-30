"""Run any command, then notify Discord of its exit status."""
import argparse
import subprocess
from discord_client import send

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', default='Task')
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args()
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not command:
        p.error('Supply a command after --')
    result = subprocess.run(command)
    send(f'{a.name}: ' + ('completed successfully.' if result.returncode == 0 else f'failed with exit code {result.returncode}.'))
    raise SystemExit(result.returncode)

if __name__ == '__main__':
    main()
