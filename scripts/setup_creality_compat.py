#!/usr/bin/env python3
"""Private Ubuntu 22.04 compatibility libraries for Creality Print 7.3.

Uses signed Ubuntu package metadata and extracts packages without installing them.
The newer loader is used only inside the slicer's isolated Bubblewrap process.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1] / '.tools/creality'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extract-only', action='store_true', help='Use packages already downloaded by the setup workflow.')
    args = parser.parse_args()
    download, destination = ROOT / 'compat-download', ROOT / 'compat'
    state = download / 'apt'
    for directory in (state / 'lists/partial', state / 'cache/archives/partial', destination):
        directory.mkdir(parents=True, exist_ok=True)
    sources = state / 'sources.list'
    sources.write_text('deb [arch=amd64] http://archive.ubuntu.com/ubuntu noble main\n')
    options = ['-o', 'Dir::State::lists=' + str(state / 'lists'), '-o', 'Dir::Cache=' + str(state / 'cache'),
               '-o', 'Dir::Etc::sourcelist=' + str(sources), '-o', 'Dir::Etc::sourceparts=-',
               '-o', 'APT::Get::List-Cleanup=0']
    if not args.extract_only:
        subprocess.run(['apt-get', *options, 'update'], cwd=download, check=True)
        subprocess.run(['apt-get', *options, 'download', 'libc6', 'libstdc++6', 'libgcc-s1',
                        'libtiff6', 'libglib2.0-0t64', 'libharfbuzz0b'], cwd=download, check=True)
        # The workstation's Jammy WebKit build uses its existing ICU/GStreamer
        # libraries; only its parallel Soup3 / WebKit4.1 libraries are added here.
        subprocess.run(['apt-get', 'download', 'libsoup-3.0-0', 'libwebkit2gtk-4.1-0',
                        'libjavascriptcoregtk-4.1-0'], cwd=download, check=True)
    packages = []
    for path in sorted(download.glob('*.deb')):
        subprocess.run(['dpkg-deb', '-x', str(path), str(destination)], check=True)
        packages.append({'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    candidates = [destination / prefix / 'x86_64-linux-gnu/ld-linux-x86-64.so.2' for prefix in ('usr/lib', 'lib')]
    loader = next((path for path in candidates if path.is_file()), None)
    if not loader or len(packages) < 9:
        parser.error('The private compatibility package set is incomplete.')
    # Creality resolves resources relative to /proc/self/exe. When invoked with
    # an explicit private loader, that location can refer to the loader itself.
    # A read-only resource mount supplies the same vendor resources at that path.
    (loader.parent.parent / 'resources').mkdir(exist_ok=True)
    (destination / 'manifest.json').write_text(json.dumps({'loader': str(loader.relative_to(destination)),
                                                          'packages': packages}, sort_keys=True, indent=2) + '\n')
    print('Extracted private compatibility libraries. System libraries are unchanged.')


if __name__ == '__main__':
    main()
