#!/usr/bin/env python3
"""Install the dashboard and printer test environments; never start services."""
import argparse
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error('Use Python 3.10 or newer.')
    for name, requirements in (('lab-agents', 'requirements.txt'),
                               ('photonics-printer-mcp-main', 'requirements-dev.txt')):
        directory = ROOT / name
        environment = directory / '.venv'
        if not environment.exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / 'bin/python'
        subprocess.run([str(python), '-m', 'pip', 'install', '-r',
                        str(directory / requirements)], check=True)
        subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
    print('Setup complete. No services or hardware connections were started.')


if __name__ == '__main__':
    main()
