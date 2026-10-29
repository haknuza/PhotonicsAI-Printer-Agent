#!/usr/bin/env python3
"""Install a checksum-pinned official Creality Print runtime locally, no printer I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = '7.3.0.6157'
FILENAME = 'CrealityPrint-V7.3.0.6157-x86_64-Release.AppImage'
URL = 'https://github.com/CrealityOfficial/CrealityPrint/releases/download/v7.3.0/' + FILENAME
SHA256 = '55dedb43928b792e311d3223bc8072402d84ed456cc05239e027a10297c89955'


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Use an already downloaded official AppImage; checksum is mandatory.')
    parser.add_argument('--compat', action='store_true', help='Also prepare private Ubuntu 22.04 compatibility libraries; no system upgrades.')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('This local runtime installer supports Linux x86_64 only.')
    destination = ROOT / '.tools/creality'
    runtime = destination / 'squashfs-root'
    manifest = destination / 'installation.json'
    if runtime.exists():
        if manifest.is_file() and json.loads(manifest.read_text()).get('archive_sha256') == SHA256:
            print('Pinned Creality Print runtime is already extracted:', runtime)
            if args.compat:
                subprocess.run([sys.executable, str(ROOT / 'scripts/setup_creality_compat.py')], check=True)
            return
        parser.error('An existing unrecognized runtime is present. Preserve it before reinstalling; nothing was overwritten.')
    archive = args.archive.resolve() if args.archive else ROOT / '.tools/creality-download' / FILENAME
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not args.archive:
        subprocess.run(['curl', '-L', '--fail', '--silent', '--show-error', '--continue-at', '-',
                        '--connect-timeout', '10', '--max-time', '1200', '--output', str(archive), URL], check=True)
    if not archive.is_file() or file_digest(archive) != SHA256:
        parser.error('Official AppImage checksum mismatch or incomplete download. No vendor executable was run.')
    destination.mkdir(parents=True, exist_ok=True)
    archive.chmod(archive.stat().st_mode | 0o100)
    subprocess.run([str(archive), '--appimage-extract'], cwd=destination, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=120, check=True)
    manifest.write_text(json.dumps({'version': VERSION, 'url': URL, 'archive_sha256': SHA256}, indent=2) + '\n')
    print('Verified and extracted official Creality Print', VERSION, 'to', runtime)
    print('No system packages, user GUI configuration or printer settings were changed.')
    if args.compat:
        subprocess.run([sys.executable, str(ROOT / 'scripts/setup_creality_compat.py')], check=True)


if __name__ == '__main__':
    main()
