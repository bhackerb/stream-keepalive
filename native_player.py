"""Play a user-supplied direct media URL without a browser page or site scripts."""
import argparse
import shutil
import subprocess
from urllib.parse import urlsplit

from resource_monitor import ResourceMonitor


def player_command(url, unmute=False, executable='mpv'):
    try:
        source = urlsplit(url)
        valid = source.scheme == 'https' and source.hostname and not source.username and not source.password and source.port in (None, 443)
    except ValueError:
        valid = False
    if not valid or any(ord(c) < 32 for c in url):
        raise ValueError('Provide a direct HTTPS media URL without embedded credentials')
    return [executable, '--no-config', '--load-scripts=no', '--ytdl=no',
            '--hwdec=auto', '--cache=yes', '--cache-secs=10',
            '--demuxer-max-bytes=64MiB', '--demuxer-max-back-bytes=8MiB',
            '--title=StreamKeeper Native', '--mute=no' if unmute else '--mute=yes', '--', url]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='Direct HTTPS HLS/DASH/video URL, not a game listing or embed page')
    parser.add_argument('--unmute', action='store_true', help='Start with sound (default: muted)')
    args = parser.parse_args()
    try:
        executable = shutil.which('mpv')
        if not executable:
            raise ValueError('Install mpv before using native playback')
        command = player_command(args.url, args.unmute, executable)
        snapshot = ResourceMonitor().sample()
        if snapshot['state'] != 'ok':
            raise ValueError('Native playback deferred: ' + snapshot['message'])
        # No shell, page scraping, cookie extraction, or external site scripts.
        return subprocess.call(command)
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
