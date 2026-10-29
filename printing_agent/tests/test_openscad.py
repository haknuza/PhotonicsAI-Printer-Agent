"""Compiler policy, subprocess lifecycle and revision storage; no printer use."""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from printing_agent.openscad import (Compilation, ModelError, ModelJobs, OpenSCADAdapter,
                                     locate_openscad, stl_dimensions, validate_source)

# A small binary triangle fixture. It is not claimed to be compiled by OpenSCAD.
TRIANGLE = b'fixture'.ljust(80, b'\0') + struct.pack('<I12fH', 1, 0, 0, 1, 0, 0, 0, 20, 0, 0, 0, 10, 5, 0)
ROOT = Path(__file__).resolve().parents[2]


class SourcePolicyTests(unittest.TestCase):
    def test_external_operations_denied_including_aliases_and_computed_paths(self):
        for source in ('include <secret.scad>;', 'use <../library.scad>;',
                       'import(str("/", "etc/passwd"));', 'surface(file="secret.png");',
                       'import_stl("secret.stl");', 'import_dxf("secret.dxf");',
                       'echo(dxf_dim(file="secret.dxf"));'):
            with self.subTest(source=source), self.assertRaises(ModelError) as error:
                validate_source(source)
            self.assertEqual(error.exception.code, 'EXTERNAL_ASSET_DENIED')

    def test_strings_comments_and_parametric_source_are_supported(self):
        source = '// include <ignored>\n/* surface("ignored"); */\necho("import \\\"text\\\""); cube([2,3,4]);'
        self.assertEqual(validate_source(source), source.encode())
        validate_source((ROOT/'examples/parametric_spacer.scad').read_text())

    def test_invalid_source_is_rejected(self):
        for source in ('', ' ', None, 'cube(1);\0', 'x' * 131073, 'ս = 1;', '/*x' * 40000, '"unterminated'):
            with self.subTest(source=source), self.assertRaises(ModelError):
                validate_source(source)

    def test_executable_configuration_and_path_fallback(self):
        with patch.dict(os.environ, {'OPENSCAD_BIN': ''}), patch('shutil.which', return_value='/usr/bin/openscad') as which:
            self.assertEqual(locate_openscad(), Path('/usr/bin/openscad'))
            which.assert_called_once_with('openscad')
        with patch.dict(os.environ, {'OPENSCAD_BIN': '/existing/custom openscad'}), patch('shutil.which', return_value='/existing/custom openscad') as which:
            locate_openscad()
            which.assert_called_once_with('/existing/custom openscad')
        with patch.dict(os.environ, {'OPENSCAD_BIN': '/not/installed'}), patch('shutil.which', return_value=None), self.assertRaises(ModelError) as error:
            locate_openscad()
        self.assertEqual(error.exception.code, 'OPENSCAD_UNAVAILABLE')
        self.assertIn('OPENSCAD_BIN', str(error.exception))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='compiler test ')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory/'output').mkdir()
        (self.directory/'model.scad').write_text('cube([20,10,5]);')
        self.script = self.directory/'fake compiler.py'
        self.script.write_text('''import os, pathlib, sys, time
mode = sys.argv[1]
output = pathlib.Path('output/model.stl')
if mode == 'timeout':
    print('WARNING: before timeout', file=sys.stderr, flush=True)
    time.sleep(10)
if mode == 'logs':
    print('x' * 100000)
if mode == 'failed':
    output.write_bytes(bytes.fromhex(sys.argv[2]))
    print('ERROR: invalid geometry', file=sys.stderr)
    sys.exit(1)
if mode == 'zero-error':
    print('ERROR: failed even with exit zero', file=sys.stderr)
if mode not in ('missing', 'zero-error'):
    output.write_bytes(b'' if mode == 'empty' else bytes.fromhex(sys.argv[2]))
print('WARNING: fixture diagnostic', file=sys.stderr)
print('env-secret=' + os.getenv('SECRET_TEST_KEY', 'absent'))
''')
        with patch('printing_agent.openscad.sys.platform', 'linux'), \
                patch('printing_agent.openscad.shutil.which', side_effect=lambda name: str(Path(sys.executable).resolve()) if '/' in name else '/usr/bin/' + name):
            self.adapter = OpenSCADAdapter(sys.executable, timeout=.4)

    def compile(self, mode):
        with patch.object(self.adapter, '_command', return_value=[sys.executable, str(self.script), mode, TRIANGLE.hex()]):
            return self.adapter.compile(self.directory)

    def test_success_warnings_spaces_and_clean_environment(self):
        with patch.dict(os.environ, {'SECRET_TEST_KEY': 'must-not-reach-compiler'}):
            result = self.compile('success')
        self.assertEqual(result.dimensions_mm, {'x':20, 'y':10, 'z':5})
        self.assertIn('WARNING:', result.stderr)
        self.assertIn('env-secret=absent', result.stdout)

    def test_failure_is_not_success_even_when_output_exists(self):
        with self.assertRaises(ModelError) as error:
            self.compile('failed')
        self.assertEqual(error.exception.code, 'COMPILE_FAILED')
        self.assertIn('invalid geometry', error.exception.stderr)

    def test_missing_empty_and_error_with_zero_exit(self):
        for mode, code in (('missing','INVALID_OUTPUT'), ('empty','INVALID_OUTPUT'), ('zero-error','COMPILE_FAILED')):
            (self.directory/'output/model.stl').unlink(missing_ok=True)
            with self.subTest(mode=mode), self.assertRaises(ModelError) as error:
                self.compile(mode)
            self.assertEqual(error.exception.code, code)

    def test_timeout_preserves_diagnostics_and_log_limit_is_bounded(self):
        for mode, code in (('timeout', 'COMPILE_TIMEOUT'), ('logs', 'LOG_LIMIT')):
            with self.subTest(mode=mode), self.assertRaises(ModelError) as error:
                self.compile(mode)
            self.assertEqual(error.exception.code, code)
            self.assertLessEqual(len(error.exception.stdout.encode())+len(error.exception.stderr.encode()), 65536)
            if mode == 'timeout': self.assertIn('before timeout', error.exception.stderr)

    def test_stale_output_and_symlink_outputs_are_rejected(self):
        output = self.directory/'output/model.stl'
        output.symlink_to(self.script)
        with self.assertRaises(ModelError): self.compile('success')

    def test_command_enforces_os_isolation_and_fixed_cli(self):
        command = self.adapter._command(self.directory)
        self.assertIn('--unshare-all', command)
        self.assertIn('--clearenv', command)
        self.assertIn('--cap-drop', command)
        self.assertEqual(command[-3:], ['-o', 'output/model.stl', 'model.scad'])
        self.assertNotIn('--share-net', command)
        self.assertNotIn('--ro-bind /', command)

    def test_unavailable_sandbox_never_falls_back_to_plain_execution(self):
        with patch('printing_agent.openscad.shutil.which', side_effect=lambda name: sys.executable if '/' in name else None), self.assertRaises(ModelError) as error:
            OpenSCADAdapter(sys.executable)
        self.assertEqual(error.exception.code, 'SANDBOX_UNAVAILABLE')

    def test_invalid_mesh_coordinates_are_rejected(self):
        output = self.directory/'output/model.stl'
        output.write_bytes(TRIANGLE[:100] + struct.pack('<f', float('nan')) + TRIANGLE[104:])
        with self.assertRaises(ModelError): stl_dimensions(output)


class FixtureAdapter:
    executable = Path('/test/fixture-compiler')
    def __init__(self, entered=None, release=None):
        self.entered, self.release = entered, release
        self.active = self.peak = 0
        self.lock = threading.Lock()
    def compile(self, directory):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.entered: self.entered.set()
            if self.release: self.release.wait(3)
            (directory/'output/model.stl').write_bytes(TRIANGLE)
            return Compilation('', 'WARNING: simulated compiler fixture', {'x':20, 'y':10, 'z':5}, 1)
        finally:
            with self.lock: self.active -= 1


def wait_for_job(service, job_id):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job['status'] not in ('queued','compiling'): return job
        time.sleep(.01)
    raise AssertionError('Test compiler did not finish')


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ModelJobs(self.temp.name, adapter=FixtureAdapter())
        self.addCleanup(self.service.close)

    def job(self, **kwargs):
        return wait_for_job(self.service, self.service.submit('cube([20,10,5]);', **kwargs)['job_id'])

    def approve(self, job):
        return self.service.approve(job['job_id'], job['source_sha256'], job['stl_sha256'], True)

    def test_revisions_approval_artifacts_and_restart(self):
        first = self.job()
        self.assertEqual(first['status'], 'ready')
        self.assertTrue(self.approve(first)['approval_current'])
        second = self.job(parent_job_id=first['job_id'])
        self.assertEqual(second['model_id'], first['model_id'])
        self.assertEqual(second['revision'], 2)
        self.assertNotEqual(first['job_id'], second['job_id'])
        self.assertIsNone(second['approval'])
        self.assertFalse(self.service.get(first['job_id'])['approval_current'])
        with self.assertRaises(ModelError): self.approve(first)
        self.assertTrue(self.approve(second)['approval_current'])
        self.assertNotEqual(self.service.artifact(first['job_id'], 'model.scad'), self.service.artifact(second['job_id'], 'model.scad'))
        self.service.close()
        restored = ModelJobs(self.temp.name, adapter=FixtureAdapter())
        self.addCleanup(restored.close)
        self.assertTrue(restored.get(second['job_id'])['approval_current'])

    def test_approval_requires_exact_hashes_boolean_and_unchanged_artifacts(self):
        job = self.job()
        for source_hash, stl_hash, confirmed in (('wrong', job['stl_sha256'], True),
                                                (job['source_sha256'], 'wrong', True),
                                                (job['source_sha256'], job['stl_sha256'], 'true')):
            with self.assertRaises(ModelError):
                self.service.approve(job['job_id'], source_hash, stl_hash, confirmed)
        path = self.service.artifact(job['job_id'], 'model.stl')
        path.write_bytes(b'changed')
        with self.assertRaises(ModelError) as error: self.approve(job)
        self.assertEqual(error.exception.code, 'ARTIFACT_CHANGED')

    def test_artifact_allowlist_ids_and_symlinks(self):
        job = self.job()
        for name in ('../metadata.json','metadata.json','output/model.stl'):
            with self.assertRaises(ModelError): self.service.artifact(job['job_id'], name)
        for job_id in ('../etc/passwd','x'*32,None):
            with self.assertRaises(ModelError): self.service.get(job_id)
        path = self.service.artifact(job['job_id'], 'model.stl')
        path.unlink()
        path.symlink_to('/etc/passwd')
        with self.assertRaises(ModelError): self.service.artifact(job['job_id'], 'model.stl')

    def test_bounded_workers_and_queue_backpressure(self):
        entered, release = threading.Event(), threading.Event()
        adapter = FixtureAdapter(entered, release)
        with tempfile.TemporaryDirectory() as temp:
            service = ModelJobs(temp, adapter=adapter, workers=1, capacity=2)
            try:
                first = service.submit('cube(1);')
                self.assertTrue(entered.wait(1))
                second = service.submit('cube(2);')
                with self.assertRaises(ModelError) as error: service.submit('cube(3);')
                self.assertEqual(error.exception.code, 'QUEUE_FULL')
                self.assertEqual(adapter.peak, 1)
                self.assertEqual(service.get(second['job_id'])['status'], 'queued')
                with self.assertRaises(ModelError): service.artifact(first['job_id'], 'model.stl')
            finally:
                release.set()
                service.close()
            self.assertEqual(service.get(second['job_id'])['status'], 'ready')

    def test_interrupted_jobs_are_not_resumed(self):
        job = self.job()
        self.service.close()
        metadata = Path(self.temp.name)/job['job_id']/'metadata.json'
        saved = json.loads(metadata.read_text())
        saved['status'] = 'compiling'
        metadata.write_text(json.dumps(saved))
        restored = ModelJobs(self.temp.name, adapter=FixtureAdapter())
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(job['job_id'])['error']['code'], 'INTERRUPTED')

    def test_invalid_source_filename_and_missing_executable_fail_before_job_creation(self):
        for filename in ('../model.scad','model.stl','a/b.scad'):
            with self.assertRaises(ModelError): self.service.submit('cube(1);',filename=filename)
        with self.assertRaises(ModelError): self.service.submit('import("secret.stl");')
        self.assertEqual(self.service.list(), [])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'OPENSCAD_BIN':'/not/installed'}):
            unavailable=ModelJobs(temp)
            try:
                self.assertFalse(unavailable.configuration()['available'])
                with self.assertRaises(ModelError): unavailable.submit('cube(1);')
                self.assertEqual(unavailable.list(), [])
            finally: unavailable.close()


@unittest.skipUnless(os.environ.get('RUN_OPENSCAD_INTEGRATION') == '1', 'Opt-in real local compiler/namespace check')
class RealOpenSCADTests(unittest.TestCase):
    def test_real_example_warnings_failure_and_sandbox_file_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ModelJobs(Path(temp)/'jobs')
            try:
                source=(ROOT/'examples/parametric_spacer.scad').read_text()
                result=wait_for_job(service,service.submit(source)['job_id'])
                self.assertEqual(result['status'],'ready',result)
                self.assertEqual(result['dimensions_mm'],{'x':24,'y':24,'z':6})
                invalid=wait_for_job(service,service.submit('cube([);')['job_id'])
                self.assertEqual(invalid['status'],'error')
                self.assertIn('ERROR:',invalid['stderr'])
                warning=wait_for_job(service,service.submit('echo(unknown_name); cube(2);')['job_id'])
                self.assertEqual(warning['status'],'ready',warning)
                self.assertIn('WARNING:',warning['stderr'])
            finally: service.close()

    def test_sandbox_hides_host_files_environment_and_network(self):
        # A trusted test executable probes the exact OS boundary used for OpenSCAD.
        # This is independent of the source-language filter.
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            secret=root/'outside-job.txt'
            secret.write_text('boundary-sentinel')
            script=root/'boundary-probe'
            script.write_text('''#!/usr/bin/python3
import os, pathlib
outside = pathlib.Path('''+repr(str(secret))+''')
print('outside=' + ('VISIBLE' if outside.exists() else 'blocked'))
print('env=' + os.getenv('SECRET_TEST_KEY', 'absent'))
try:
    pathlib.Path('model.scad').write_text('modified')
    print('source=WRITABLE')
except OSError:
    print('source=readonly')
print('network_ns=' + os.readlink('/proc/self/ns/net'))
pathlib.Path('output/model.stl').write_bytes(bytes.fromhex('''+repr(TRIANGLE.hex())+'''))
''')
            script.chmod(0o700)
            job=root/'job'
            job.mkdir()
            (job/'output').mkdir()
            (job/'model.scad').write_text('cube(1);')
            with patch.dict(os.environ, {'SECRET_TEST_KEY':'not-for-compiler'}):
                result=OpenSCADAdapter(str(script)).compile(job)
            self.assertIn('outside=blocked',result.stdout)
            self.assertIn('env=absent',result.stdout)
            self.assertIn('source=readonly',result.stdout)
            self.assertNotIn('network_ns='+os.readlink('/proc/self/ns/net'),result.stdout)
