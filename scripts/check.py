#!/usr/bin/env python3
"""Run retained suites with mock hardware, local sockets and stubbed SSH."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    environment = dict(os.environ, PRINTER_MODE='mock', PRINTER_ALLOW_MOTION='0',
                       PRINTER_HOMING_MODE='manual', FASTMCP_CHECK_FOR_UPDATES='off')
    printer_python = ROOT / 'photonics-printer-mcp-main/.venv/bin/python'
    dashboard_python = ROOT / 'lab-agents/.venv/bin/python'
    if not printer_python.exists() or not dashboard_python.exists():
        raise SystemExit('Run python3 scripts/setup.py first.')
    subprocess.run([str(printer_python), '-m', 'unittest', 'discover', '-s',
                    'printing_agent/tests', '-v'], cwd=ROOT, env=environment, check=True)
    for name, python in (('photonics-printer-mcp-main', printer_python),
                         ('lab-agents', dashboard_python)):
        subprocess.run([str(python), '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                       cwd=ROOT / name, env=environment, check=True)
        subprocess.run([str(python), '-m', 'pip', 'check'], check=True)


if __name__ == '__main__':
    main()
