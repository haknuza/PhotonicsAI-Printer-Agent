"""Bundled preset provenance and isolated Creality CLI; fixtures, no devices."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from printing_agent.creality import CrealityPrint, FILAMENT, MACHINE, PROCESS, VendorPresets, resolved_profile
from printing_agent.openscad import ModelError
from test_print_jobs import GCODE


def fixture_resources(root):
    resources = root / 'resources'
    values = {
        'machine': {'type': 'machine', 'name': MACHINE, 'printer_model': 'Creality Ender-3 V3 KE',
                    'nozzle_diameter': ['0.4'], 'inherits': 'fixture_machine_base',
                    'machine_start_gcode': 'UNREVIEWED_VENDOR_START'},
        'process': {'type': 'process', 'name': PROCESS, 'inherits': 'fixture_process_base',
                    'enable_arc_fitting': '1', 'inner_wall_speed': '300',
                    'post_process': ['unreviewed external command']},
    }
    for kind in ('machine', 'process', 'filament'):
        path = resources / 'profiles/Creality' / kind
        path.mkdir(parents=True)
        base = {'type': kind, 'name': 'fixture_' + kind + '_base', 'fixture_inherited_value': 'preserved'}
        (path / (base['name'] + '.json')).write_text(json.dumps(base))
        names = FILAMENT.values() if kind == 'filament' else [values[kind]['name']]
        for name in names:
            value = values[kind] if kind in values else {'type': kind, 'name': name,
                'inherits': 'fixture_filament_base', 'filament_flow_ratio': ['0.95']}
            (path / (name + '.json')).write_text(json.dumps(value))
    return resources


class CrealityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resources = fixture_resources(self.root)
        self.presets = VendorPresets(self.resources / 'profiles')
        self.binary = self.root / 'CrealityPrint'
        self.binary.write_text('#!/bin/sh\nexit 0\n')
        self.binary.chmod(0o700)
        # These are preset/CLI fixture tests. Never discover installed OpenSCAD,
        # Bubblewrap or prlimit, or require Linux just to build a command list.
        runner = SimpleNamespace(prlimit='/fixture/prlimit', bwrap='/fixture/bwrap',
                                 _run=Mock(side_effect=AssertionError('Unexpected slicer execution')))
        guard = patch('printing_agent.creality.OpenSCADAdapter', return_value=runner)
        guard.start()
        self.addCleanup(guard.stop)

    def adapter(self):
        return CrealityPrint(str(self.binary), str(self.resources))

    def test_vendor_inheritance_is_flattened_and_hashed_with_explicit_overrides(self):
        for identifier in ('ender3v3ke-pla-04', 'ender3v3ke-petg-04'):
            profile, source = resolved_profile(identifier, self.presets, 'a' * 64)
            bundle = json.loads(source)
            self.assertEqual(profile['slicer_engine'], 'creality-print')
            self.assertEqual(profile['settings_sha256'], hashlib.sha256(source.encode()).hexdigest())
            for kind in ('machine', 'process', 'filament'):
                self.assertNotIn('inherits', bundle[kind])
                self.assertEqual(bundle[kind]['fixture_inherited_value'], 'preserved')
            self.assertEqual(bundle['process']['post_process'], [])
            self.assertEqual(bundle['process']['inner_wall_speed'], '40')
            self.assertEqual(bundle['process']['enable_arc_fitting'], '0')
            self.assertEqual(bundle['filament']['filament_flow_ratio'], ['0.95'])
            self.assertIn('BED_MESH_PROFILE LOAD=default', bundle['machine']['machine_start_gcode'])
            self.assertNotIn('UNREVIEWED', bundle['machine']['machine_start_gcode'])
            self.assertEqual(bundle['machine']['machine_end_gcode'].splitlines(), [
                'M104 S0', 'M140 S0', 'M107', 'G91', 'G1 Z5 F300', 'G90',
                'G1 X0 Y0 F1800', 'M84'])
            self.assertGreaterEqual(len(bundle['vendor_sources']), 6)

    def test_profile_and_binary_changes_invalidate_exact_settings(self):
        adapter = self.adapter()
        first = adapter.profile_for('ender3v3ke-petg-04')[0]
        filename = self.resources / 'profiles/Creality/filament' / (FILAMENT['PETG'] + '.json')
        value = json.loads(filename.read_text())
        value['filament_flow_ratio'] = ['0.97']
        filename.write_text(json.dumps(value))
        second = adapter.profile_for('ender3v3ke-petg-04')[0]
        self.assertNotEqual(first['settings_sha256'], second['settings_sha256'])
        self.binary.write_text('#!/bin/sh\nexit 1\n# changed runtime\n')
        third = adapter.profile_for('ender3v3ke-petg-04')[0]
        self.assertNotEqual(second['engine_sha256'], third['engine_sha256'])

    def test_cyclic_missing_symlink_and_wrong_printer_profiles_fail_closed(self):
        path = self.resources / 'profiles/Creality/machine' / (MACHINE + '.json')
        original = path.read_text()
        value = json.loads(original)
        for change in ({'inherits': MACHINE}, {'inherits': '../outside'},
                       {'printer_model': 'Different printer'}, {'nozzle_diameter': ['0.6']}):
            path.write_text(json.dumps(dict(value, **change)))
            with self.assertRaises(ModelError):
                self.adapter()
        path.write_text(original)
        target = self.root / 'outside.json'
        target.write_text(original)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(ModelError):
            self.adapter()

    def test_missing_explicit_creality_executable_never_falls_back(self):
        with patch.dict(os.environ, {'CREALITY_PRINT_BIN': '/missing/CrealityPrint',
                                    'PRUSA_SLICER_BIN': '/usr/bin/true'}), self.assertRaises(ModelError) as error:
            CrealityPrint()
        self.assertEqual(error.exception.code, 'CREALITY_UNAVAILABLE')

    def test_command_has_fixed_arguments_clean_environment_and_no_hardware_access(self):
        with patch('subprocess.run') as process:
            adapter = self.adapter()
            process.assert_not_called()
        directory = self.root / 'job'
        command = adapter._command(directory)
        self.assertIn('--unshare-all', command)
        self.assertIn('--clearenv', command)
        self.assertNotIn('--share-net', command)
        self.assertIn('--slice', command)
        self.assertIn('/work/configs/machine.json;/work/configs/process.json', command)
        self.assertNotIn(str(Path.home()), command)
        self.assertNotIn('PRUSA_SLICER_BIN', ' '.join(command))

    def test_no_output_is_failure_even_after_successful_cli_exit(self):
        adapter = self.adapter()
        directory = self.root / 'job'
        directory.mkdir()
        (directory / 'output').mkdir()
        (directory / 'model.stl').write_bytes(b'fixture')
        (directory / 'profile.json').write_text(adapter.profile_for('ender3v3ke-petg-04')[1])
        with patch.object(adapter.runner, '_run', return_value=('GUI started but no slicing', '')):
            with self.assertRaises(ModelError) as error:
                adapter.slice(directory)
        self.assertEqual(error.exception.code, 'CREALITY_NO_OUTPUT')

    def test_expected_plain_output_is_normalized_and_returned(self):
        adapter = self.adapter()
        directory = self.root / 'job'
        directory.mkdir()
        (directory / 'output').mkdir()
        (directory / 'model.stl').write_bytes(b'fixture')
        (directory / 'profile.json').write_text(adapter.profile_for('ender3v3ke-pla-04')[1])
        def output(*args):
            (directory / 'output/plate_1.gcode').write_bytes(GCODE)
            return 'fixture diagnostics', ''
        with patch.object(adapter.runner, '_run', side_effect=output):
            self.assertEqual(adapter.slice(directory)[0], 'fixture diagnostics')
        self.assertEqual((directory / 'output/print.gcode').read_bytes(), GCODE)

    def test_installer_rejects_unverified_archive_before_running_it(self):
        path = Path(__file__).resolve().parents[2] / 'scripts/setup_creality.py'
        spec = importlib.util.spec_from_file_location('fixture_creality_setup', path)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        archive = self.root / 'unverified.AppImage'
        archive.write_bytes(b'untrusted incomplete archive')
        with patch.object(installer, 'ROOT', self.root), \
                patch('sys.argv', ['setup_creality.py', '--archive', str(archive)]), \
                patch('subprocess.run') as process, patch('sys.stderr'), self.assertRaises(SystemExit):
            installer.main()
        process.assert_not_called()
