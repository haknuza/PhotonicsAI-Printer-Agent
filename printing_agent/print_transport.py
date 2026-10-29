"""Explicit SSH print-job transport; uses only an operator-authenticated master."""
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

from .openscad import ModelError


class SSHPrintTransport:
    simulated = False

    def __init__(self, host, control_path='/tmp/photonics-printer-control'):
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
            raise ModelError('PRINTER_CONFIGURATION', 'Provide the printer hostname or IP explicitly.', 503)
        if not isinstance(control_path, str) or not control_path.startswith('/') or any(ord(c) < 32 for c in control_path):
            raise ModelError('PRINTER_CONFIGURATION', 'Use an absolute SSH control socket path.', 503)
        self.host, self.control_path = host, control_path
        self.target_id = hashlib.sha256(('ender3v3ke:' + host).encode()).hexdigest()

    def _arguments(self):
        return ['ssh', '-F', '/dev/null', '-S', self.control_path, '-o', 'BatchMode=yes',
                '-o', 'ControlMaster=no', '-o', 'ProxyCommand=false',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=5']

    def _source(self):
        root = Path(__file__).resolve().parents[1]
        result = 'import sys, types\n'
        for name in ('printer_policy', 'printer_watchdog'):
            source = (root / 'photonics-printer-mcp-main' / (name + '.py')).read_text()
            result += (f'module = types.ModuleType({name!r})\n'
                       f'sys.modules[{name!r}] = module\nexec({source!r}, module.__dict__)\n')
        result += 'exec(' + repr(Path(__file__).with_name('print_remote.py').read_text()) + ", {'__name__': '__main__'})\n"
        return result

    def _request(self, payload):
        try:
            check = subprocess.run(self._arguments() + ['-O', 'check', 'root@' + self.host],
                                   capture_output=True, timeout=5)
            if check.returncode:
                raise ModelError('SSH_SESSION_REQUIRED', 'Open printerctl connect in a local terminal and leave it open.', 503)
            bootstrap = "import json,sys; exec(compile(json.loads(sys.stdin.buffer.readline(1048577)), '<print-helper>', 'exec'), {'__name__':'__main__'})"
            remote = '/usr/share/klippy-env/bin/python -c ' + shlex.quote(bootstrap)
            result = subprocess.run(self._arguments() + ['root@' + self.host, remote],
                                    input=json.dumps(self._source()) + '\n' + json.dumps(payload, allow_nan=False),
                                    capture_output=True, text=True, timeout=35)
            if result.returncode or len(result.stdout) > 1024 * 1024:
                raise ValueError('SSH response unavailable')
            reply = json.loads(result.stdout)
            if not isinstance(reply, dict) or type(reply.get('ok')) is not bool or reply.get('simulated') is not False:
                raise ValueError('Invalid print helper response')
            if not reply['ok']:
                code = 'SUBMISSION_UNCERTAIN' if reply.get('command_may_have_executed') else 'PRINTER_REJECTED'
                raise ModelError(code, reply.get('error', 'Printer rejected the job.'), 409)
            return reply
        except ModelError:
            raise
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            code = 'SUBMISSION_UNCERTAIN' if payload['action'] == 'submit' else 'PRINTER_UNAVAILABLE'
            raise ModelError(code, 'Printer connection outcome is uncertain. Inspect the printer; do not retry a start.', 503) from exc

    def status(self):
        return self._request({'action': 'status'})['state']

    def submit(self, job_id, data, configuration_sha256):
        return self._request({'action': 'submit', 'job_id': job_id,
                              'sha256': hashlib.sha256(data).hexdigest(),
                              'configuration_sha256': configuration_sha256,
                              'gcode': base64.b64encode(data).decode('ascii')})
