"""Exact-job print approvals and native helper behavior; never physical devices."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# The remote bootstrap injects this retained standalone module; make that
# dependency explicit for isolated discovery of the helper's fixture tests too.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'photonics-printer-mcp-main'))

from printing_agent.openscad import ModelError, ModelJobs
from printing_agent.print_jobs import PrintJobs
from printing_agent.print_transport import SSHPrintTransport
from printing_agent.slicing import inspect_gcode, profile_info
from printing_agent import print_remote
from test_openscad import FixtureAdapter, wait_for_job

GCODE = b'''G90
M83
G28
BED_MESH_PROFILE LOAD=default
M140 S60
M104 S210
M190 S60
M109 S210
G1 Z0.2 F300
G1 X100 Y100 F1200
G1 X110 Y100 E1 F1200
G1 X110 Y110 E1
M104 S0
M140 S0
M107
G91
G1 Z5 F300
G90
G1 X0 Y0 F1800
M84
; estimated printing time (normal mode) = 10s
; filament used [mm] = 2
'''


class FixtureSlicer:
    engine_id = 'fixture'

    @staticmethod
    def profile_for(profile_id):
        profile = profile_info(profile_id)
        source = json.dumps({'fixture_profile': profile_id}, sort_keys=True) + '\n'
        profile['settings_sha256'] = hashlib.sha256(source.encode()).hexdigest()
        return profile, source

    def slice(self, directory):
        (Path(directory) / 'output/print.gcode').write_bytes(GCODE)


class FixtureTransport:
    simulated = True  # Tests only. No production fake-success backend exists.
    target_id = 'fixture-printer'

    def __init__(self):
        self.calls = []
        self.state = {'ready_for_print': True, 'configuration_sha256': 'a' * 64,
                      'printer_identity': 'fixture-ender', 'readiness_error': None,
                      'print_stats': {'state': 'standby', 'filename': ''},
                      'extruder': {'temperature': 21, 'target': 0},
                      'heater_bed': {'temperature': 22, 'target': 0},
                      'virtual_sdcard': {'progress': 0}}

    def status(self):
        return copy.deepcopy(self.state)

    def submit(self, job_id, data, configuration_sha256):
        self.calls.append((job_id, data, configuration_sha256))
        self.state['print_stats'] = {'state': 'printing', 'filename': 'photonics_' + job_id + '.gcode'}
        return {'submission': 'accepted', 'filename': 'photonics_' + job_id + '.gcode'}


def wait_print(service, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = service.get(job_id)
        if result['status'] not in ('queued', 'slicing'):
            return result
        time.sleep(.01)
    raise AssertionError('Fixture slicing timeout')


class PrintJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models = ModelJobs(Path(self.temp.name) / 'models', adapter=FixtureAdapter())
        self.addCleanup(self.models.close)
        self.transport = FixtureTransport()
        self.prints = PrintJobs(Path(self.temp.name) / 'prints', self.models, self.transport, FixtureSlicer())
        self.addCleanup(self.prints.close)
        model = self.models.submit('cube([20,10,5]);')
        self.model = wait_for_job(self.models, model['job_id'])

    def approve_model(self):
        return self.models.approve(self.model['job_id'], self.model['source_sha256'],
                                   self.model['stl_sha256'], True)

    def prepared(self):
        self.approve_model()
        job = self.prints.prepare(self.model['job_id'], 'ender3v3ke-pla-04')
        result = wait_print(self.prints, job['job_id'])
        self.assertEqual(result['status'], 'ready', result)
        return result

    def approve(self, job):
        return self.prints.approve(job['job_id'], job['gcode_sha256'], True, True)

    def test_model_approval_and_slicing_never_submit(self):
        with self.assertRaises(ModelError) as error:
            self.prints.prepare(self.model['job_id'], 'ender3v3ke-pla-04')
        self.assertEqual(error.exception.code, 'MODEL_APPROVAL_REQUIRED')
        job = self.prepared()
        self.assertEqual(self.transport.calls, [])
        self.assertIsNone(job['approval'])
        self.assertEqual(job['summary']['extrusion_moves'], 2)
        self.assertEqual(self.prints.artifact(job['job_id']).read_bytes(), GCODE)

    def test_fresh_confirmation_exact_hash_and_one_shot_start(self):
        job = self.prepared()
        for confirmed, setup in ((False, True), (True, False), ('true', True)):
            with self.assertRaises(ModelError):
                self.prints.approve(job['job_id'], job['gcode_sha256'], confirmed, setup)
        with self.assertRaises(ModelError):
            self.prints.approve(job['job_id'], 'b' * 64, True, True)
        with self.assertRaises(ModelError):
            self.prints.start(job['job_id'], 'invented-token')
        approved = self.approve(job)
        self.assertEqual(self.transport.calls, [])
        started = self.prints.start(job['job_id'], approved['token'])
        self.assertEqual(started['status'], 'submitted')
        self.assertEqual(self.transport.calls[0][1], GCODE)
        with self.assertRaises(ModelError):
            self.prints.start(job['job_id'], approved['token'])
        with self.assertRaises(ModelError):
            self.approve(job)
        self.assertEqual(len(self.transport.calls), 1)

    def test_expiry_cross_job_and_reapproval_invalidate_tokens(self):
        job = self.prepared()
        approved = self.approve(job)
        self.prints.tokens[approved['token']]['expires_at'] = time.time() - 1
        with self.assertRaises(ModelError):
            self.prints.start(job['job_id'], approved['token'])
        old = self.approve(job)
        new = self.approve(job)
        with self.assertRaises(ModelError):
            self.prints.start(job['job_id'], old['token'])
        second = wait_print(self.prints, self.prints.prepare(self.model['job_id'], 'ender3v3ke-pla-04')['job_id'])
        with self.assertRaises(ModelError):
            self.prints.start(second['job_id'], new['token'])
        self.assertEqual(self.transport.calls, [])

    def test_new_model_revision_tampered_gcode_and_target_changes_block_start(self):
        for mutation in ('model', 'gcode', 'target'):
            with self.subTest(mutation=mutation):
                job = self.prepared()
                approved = self.approve(job)
                if mutation == 'model':
                    self.models.submit('cube(3);', parent_job_id=self.model['job_id'])
                elif mutation == 'gcode':
                    self.prints.artifact(job['job_id']).write_bytes(GCODE + b';tampered\n')
                else:
                    self.transport.target_id = 'changed-target'
                with self.assertRaises(ModelError):
                    self.prints.start(job['job_id'], approved['token'])
                self.assertEqual(self.transport.calls, [])
                if mutation == 'model':
                    # Use a separate approved model for subsequent subcases.
                    new = self.models.submit('cube([20,10,5]);')
                    self.model = wait_for_job(self.models, new['job_id'])

    def test_busy_or_changed_configuration_blocks_approval(self):
        job = self.prepared()
        self.transport.state['ready_for_print'] = False
        with self.assertRaises(ModelError):
            self.approve(job)
        self.transport.state.update(ready_for_print=True, configuration_sha256='c' * 64)
        with self.assertRaises(ModelError):
            self.approve(job)
        self.assertEqual(self.transport.calls, [])

    def test_mutated_slicer_input_cannot_gain_or_reuse_approval(self):
        for name in ('profile.json', 'model.stl'):
            with self.subTest(name=name):
                job = self.prepared()
                approved = self.approve(job)
                (self.prints.root / job['job_id'] / name).write_bytes(b'changed input')
                with self.assertRaises(ModelError):
                    self.prints.start(job['job_id'], approved['token'])
        self.assertEqual(self.transport.calls, [])

    def test_uncertain_submission_is_never_retried_or_replayed_after_restart(self):
        job = self.prepared()
        approved = self.approve(job)
        with patch.object(self.transport, 'submit', side_effect=TimeoutError('fixture')) as submit:
            with self.assertRaises(ModelError):
                self.prints.start(job['job_id'], approved['token'])
            self.assertEqual(self.prints.get(job['job_id'])['status'], 'uncertain')
            with self.assertRaises(ModelError):
                self.prints.start(job['job_id'], approved['token'])
            submit.assert_called_once()
        self.prints.close()
        restored = PrintJobs(self.prints.root, self.models, self.transport, FixtureSlicer())
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(job['job_id'])['status'], 'uncertain')
        self.assertEqual(restored.tokens, {})
        self.assertEqual(self.transport.calls, [])

    def test_previous_slicer_jobs_require_fresh_preparation_but_pending_prints_stay_uncertain(self):
        job = self.prepared()
        old = self.prints.jobs[job['job_id']]
        old.pop('slicer_engine')  # Legacy PrusaSlicer metadata had no engine field.
        self.prints._save(old)
        self.prints.close()
        restored = PrintJobs(self.prints.root, self.models, self.transport, FixtureSlicer())
        self.addCleanup(restored.close)
        result = restored.get(job['job_id'])
        self.assertEqual(result['error']['code'], 'SLICER_CHANGED')
        with self.assertRaises(ModelError):
            restored.approve(job['job_id'], job['gcode_sha256'], True, True)
        restored.jobs[job['job_id']]['status'] = 'submitted'
        restored._save(restored.jobs[job['job_id']])
        restored.close()
        pending = PrintJobs(self.prints.root, self.models, self.transport, FixtureSlicer())
        self.addCleanup(pending.close)
        self.assertEqual(pending.get(job['job_id'])['status'], 'uncertain')
        self.assertEqual(self.transport.calls, [])

    def test_progress_matches_only_the_originating_filename(self):
        job = self.prepared()
        self.prints.start(job['job_id'], self.approve(job)['token'])
        self.transport.state['print_stats'] = {'state': 'complete', 'filename': 'unrelated.gcode'}
        self.assertEqual(self.prints.refresh(job['job_id'])['status'], 'submitted')
        self.transport.state['print_stats']['filename'] = 'photonics_' + job['job_id'] + '.gcode'
        self.assertEqual(self.prints.refresh(job['job_id'])['status'], 'completed')


class GcodeTests(unittest.TestCase):
    def test_completion_returns_xy_home_after_lift_with_heaters_off(self):
        summary = inspect_gcode(GCODE, profile_info('ender3v3ke-pla-04'))
        self.assertEqual(summary['bounds_mm']['x'], [0, 110])
        self.assertEqual(summary['bounds_mm']['y'], [0, 110])
        self.assertEqual(summary['bounds_mm']['z'], [0.2, 5.2])
        self.assertEqual(summary['extrusion_moves'], 2)

    def test_missing_changed_or_followed_completion_sequence_is_rejected(self):
        for change in (
            GCODE.replace(b'G1 X0 Y0 F1800\n', b''),
            GCODE.replace(b'G1 X0 Y0 F1800', b'G28'),
            GCODE.replace(b'G1 X0 Y0 F1800', b'G28 X Y'),
            GCODE.replace(b'G1 X0 Y0 F1800', b'G1 X0 Y0 Z0 F1800'),
            GCODE.replace(b'G1 Z5 F300', b'G1 Z-5 F300'),
            GCODE.replace(b'G1 Z5 F300\nG90\nG1 X0 Y0 F1800', b'G90\nG1 X0 Y0 F1800\nG1 Z5 F300'),
            GCODE.replace(b'M104 S0', b'M104 S210'),
            GCODE + b'G1 Z0 F300\n',
            GCODE.replace(b'G1 X110 Y110 E1', b'G28\nG1 X110 Y110 E1'),
        ):
            with self.subTest(gcode=change), self.assertRaises(ModelError):
                inspect_gcode(change, profile_info('ender3v3ke-pla-04'))

    def test_completion_lift_keeps_tall_prints_inside_z_limit(self):
        profile = profile_info('ender3v3ke-pla-04')
        at_limit = GCODE.replace(b'G1 Z0.2', b'G1 Z240')
        self.assertEqual(inspect_gcode(at_limit, profile)['bounds_mm']['z'], [240, 245])
        with self.assertRaises(ModelError):
            inspect_gcode(GCODE.replace(b'G1 Z0.2', b'G1 Z240.2'), profile)

    def test_fixed_profile_and_toolpath_reject_unsupported_or_unbounded_output(self):
        profile = profile_info('ender3v3ke-pla-04')
        for forbidden in (b'G1 X999', b'M104 S300', b'RUN_SHELL_COMMAND CMD=anything',
                          b'G92 X0', b'SAVE_CONFIG', b'G1 Xnan', b'G1 X20 X30', b'M204 S9999'):
            with self.subTest(command=forbidden), self.assertRaises(ModelError):
                inspect_gcode(GCODE.replace(b'G1 X110 Y100 E1 F1200', forbidden), profile)
        with self.assertRaises(ModelError):
            profile_info('unknown')

    def test_constructor_has_no_network_side_effect_and_requires_explicit_target(self):
        with patch('subprocess.run') as process:
            connection = SSHPrintTransport('192.0.2.10')
            process.assert_not_called()
            self.assertFalse(connection.simulated)
        for host in ('', 'host;anything', '-option', None):
            with self.assertRaises(ModelError):
                SSHPrintTransport(host)


class RemotePrintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS commonly places temporary directories under a symlinked /var.
        # The fixture must satisfy the real helper's canonical-directory policy.
        self.root = Path(self.temp.name).resolve()
        self.patch = patch.object(print_remote, 'LOCK_PATH', str(self.root / 'lock'))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.commands = []
        self.state = {
            'webhooks': {'state': 'ready'}, 'print_stats': {'state': 'standby'},
            'virtual_sdcard': {'is_active': False}, 'pause_resume': {'is_paused': False},
            'toolhead': {'axis_minimum': [-12, -20, -5], 'axis_maximum': [221, 223, 246]},
            'motion_report': {'live_velocity': 0}, 'heater_bed': {'target': 0}, 'extruder': {'target': 0},
            'gcode_move': {'speed_factor': 1, 'extrude_factor': 1, 'homing_origin': [0, 0, 0, 0]},
            'bed_mesh': {'profiles': {'default': {}}},
            'configfile': {'settings': {'virtual_sdcard': {'path': str(self.root)},
                         'extruder': {'nozzle_diameter': .4, 'filament_diameter': 1.75}}}}
        outer = self

        class FixtureAPI:
            def call(self, method, params=None, **kwargs):
                if method == 'objects/query':
                    return {'status': copy.deepcopy(outer.state)}
                if method == 'info':
                    return {'hostname': 'fixture-ender'}
                if method == 'gcode/script':
                    outer.commands.append(params['script'])
                    return {}
                raise AssertionError(method)

            def close(self):
                pass

        self.api = FixtureAPI

    def request(self):
        result = print_remote.handle({'action': 'status'}, self.api)
        self.assertTrue(result['ok'], result)
        state = result['state']
        return {'action': 'submit', 'job_id': 'd' * 32, 'gcode': base64.b64encode(GCODE).decode(),
                'sha256': hashlib.sha256(GCODE).hexdigest(),
                'configuration_sha256': state['configuration_sha256']}

    def test_status_reads_do_not_write_files_or_dispatch_gcode(self):
        result = print_remote.handle({'action': 'status'}, self.api)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['state']['ready_for_print'])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.commands, [])

    def test_virtual_sd_directory_symlinks_are_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.state['configfile']['settings']['virtual_sdcard']['path'] = str(alias)
        result = print_remote.handle({'action': 'status'}, self.api)
        self.assertFalse(result['ok'])
        self.assertIn('without symlinks', result['error'])
        self.assertEqual(self.commands, [])

    def test_native_submit_hash_marker_fixed_command_and_replay_protection(self):
        request = self.request()
        reply = print_remote.handle(request, self.api)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual((self.root / reply['filename']).read_bytes(), GCODE)
        self.assertEqual(self.commands, ['SDCARD_PRINT_FILE FILENAME=photonics_' + 'd' * 32 + '.gcode'])
        self.assertFalse(print_remote.handle(request, self.api)['ok'])
        self.assertEqual(len(self.commands), 1)

    def test_preflight_rejects_busy_heat_overrides_and_config_changes(self):
        request = self.request()
        for section, field, value in (('print_stats', 'state', 'printing'),
                                      ('virtual_sdcard', 'is_active', True),
                                      ('pause_resume', 'is_paused', True),
                                      ('motion_report', 'live_velocity', 2),
                                      ('heater_bed', 'target', 60),
                                      ('gcode_move', 'speed_factor', 2)):
            with self.subTest(field=field):
                old = self.state[section][field]
                self.state[section][field] = value
                self.assertFalse(print_remote.handle(request, self.api)['ok'])
                self.state[section][field] = old
        request['configuration_sha256'] = 'b' * 64
        self.assertFalse(print_remote.handle(request, self.api)['ok'])
        self.assertEqual(self.commands, [])

    def test_wrong_hash_and_traversal_are_rejected(self):
        request = self.request()
        request['sha256'] = 'b' * 64
        self.assertFalse(print_remote.handle(request, self.api)['ok'])
        request['job_id'] = '../outside'
        with self.assertRaises(ValueError):
            print_remote.handle(request, self.api)
        self.assertEqual(self.commands, [])


@unittest.skipUnless(__import__('os').environ.get('RUN_SLICER_INTEGRATION') == '1',
                     'Opt-in real local compiler/slicer test; no printer')
class RealSlicerTests(unittest.TestCase):
    def test_isolated_openscad_to_slicer(self):
        from printing_agent.openscad import OpenSCADAdapter
        from printing_agent.creality import CrealityPrint
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'output').mkdir()
            (path / 'model.scad').write_text('cube([20,10,5]);')
            OpenSCADAdapter().compile(path)
            (path / 'model.stl').write_bytes((path / 'output/model.stl').read_bytes())
            for name in ('ender3v3ke-pla-04', 'ender3v3ke-petg-04'):
                with tempfile.TemporaryDirectory() as slicing:
                    target = Path(slicing)
                    (target / 'output').mkdir()
                    (target / 'model.stl').write_bytes((path / 'model.stl').read_bytes())
                    slicer = CrealityPrint()
                    profile, source = slicer.profile_for(name)
                    (target / 'profile.json').write_text(source)
                    try:
                        slicer.slice(target)
                    except ModelError as exc:
                        self.fail(f'{exc.code}: {exc}\n{exc.stdout}\n{exc.stderr}')
                    data = (target / 'output/print.gcode').read_bytes()
                    summary = inspect_gcode(data, profile)
                    self.assertIn(b'G1 Z5 F300\nG90\nG1 X0 Y0 F1800\nM84', data)
                    self.assertGreater(summary['extrusion_moves'], 10)
                    self.assertNotEqual(summary['estimated_time'], 'Unavailable')
                    self.assertGreater(summary['filament_mm'], 0)
                    self.assertEqual(summary['bounds_mm']['x'][0], 0)
                    self.assertEqual(summary['bounds_mm']['y'][0], 0)
                    # The travel bounds now include the return home; evaluate
                    # centering/orientation from the extrusion paths instead.
                    segments = summary['preview_segments']
                    xs = [point for segment in segments for point in (segment[0], segment[2])]
                    ys = [point for segment in segments for point in (segment[1], segment[3])]
                    x, y = [min(xs), max(xs)], [min(ys), max(ys)]
                    self.assertAlmostEqual(sum(x) / 2, 110, delta=1)
                    self.assertAlmostEqual(sum(y) / 2, 110, delta=1)
                    self.assertAlmostEqual((x[1] - x[0]) - (y[1] - y[0]), 10, delta=1)
