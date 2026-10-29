"""Absolute-target regression checks; all printer access is simulated."""
import contextlib
import io
import math
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from printer_client import MockPrinter, PrinterError, mock_state, run_prepared
from printer_policy import SafetyError, check_execution_state, plan_absolute
from printer_watchdog import execute, validate_request
from simple_runner import run
from test_simple import FakeAPI


TARGET = {'x': 110, 'y': 120, 'z': 15}


class AbsolutePolicyTests(unittest.TestCase):
    def test_straight_absolute_target_feed_and_duration(self):
        result = plan_absolute(mock_state(), TARGET, 5)
        self.assertEqual(result['start'], [100, 100, 5])
        self.assertEqual(result['end'], [110, 120, 15])
        self.assertEqual(result['targets'], [[110, 120, 15]])
        self.assertAlmostEqual(result['nominal_seconds'], math.sqrt(600) / 5)
        self.assertEqual(result['script'], 'G90\nG1 X110.000 Y120.000 Z15.000 F300.000000\nM400')
        self.assertEqual(result['z_range'], [5, 15])

    def test_all_axes_homed_even_when_z_does_not_change(self):
        for axes in ('', 'x', 'xy', 'xz', 'yz'):
            state = mock_state()
            state['toolhead']['homed_axes'] = axes
            with self.subTest(axes=axes), self.assertRaisesRegex(SafetyError, 'Home all axes'):
                plan_absolute(state, {'x': 110, 'y': 100, 'z': 5}, 5)

    def test_invalid_targets_requested_and_rounded_bounds(self):
        invalid = [None, [110, 120, 15], {'x': 110, 'y': 120}, dict(TARGET, e=1),
                   dict(TARGET, x=True), dict(TARGET, y='120'), dict(TARGET, z=float('inf')),
                   dict(TARGET, x=float('nan')), dict(TARGET, x=-12.0001),
                   dict(TARGET, x=221.0001), dict(TARGET, y=223.0001),
                   dict(TARGET, z=246.0001), dict(TARGET, z=-5.0001)]
        for target in invalid:
            with self.subTest(target=target), self.assertRaises(SafetyError):
                plan_absolute(mock_state(), target, 5)
        self.assertEqual(plan_absolute(mock_state(), {'x': 200, 'y': 0, 'z': 235}, 20)['end'],
                         [200, 0, 235])

    def test_firmware_travel_and_velocity_and_operator_start_limits(self):
        state = mock_state()
        state['toolhead']['axis_maximum'][0] = 105
        with self.assertRaisesRegex(SafetyError, 'allowed travel bounds'):
            plan_absolute(state, TARGET, 5)
        state = mock_state()
        state['toolhead']['max_velocity'] = 4
        with self.assertRaisesRegex(SafetyError, 'firmware velocity limit'):
            plan_absolute(state, TARGET, 5)
        self.assertEqual(plan_absolute(state, TARGET, 4)['speed_mm_s'], 4)
        state = mock_state()
        for obj, field in (('toolhead', 'position'), ('motion_report', 'live_position'),
                           ('gcode_move', 'position'), ('gcode_move', 'gcode_position')):
            state[obj][field][0] = -13
        with self.assertRaisesRegex(SafetyError, 'outside allowed travel bounds'):
            plan_absolute(state, TARGET, 5)

    def test_invalid_speed_and_deadline(self):
        for speed in (True, '5', 0, -1, 30.01, 31, float('nan'), float('inf'), 1e-15):
            with self.subTest(speed=speed), self.assertRaises(SafetyError):
                plan_absolute(mock_state(), TARGET, speed)
        with self.assertRaisesRegex(SafetyError, 'deadline'):
            plan_absolute(mock_state(), TARGET, 0.01)

    def test_mesh_offsets_heating_and_motion_are_rejected(self):
        changes = [('bed_mesh', 'profile_name', 'default'),
                   ('gcode_move', 'homing_origin', [1, 0, 0]),
                   ('heater_bed', 'target', 60), ('extruder', 'target', 200),
                   ('print_stats', 'state', 'printing'), ('motion_report', 'live_velocity', 1)]
        for obj, field, value in changes:
            state = mock_state()
            state[obj][field] = value
            with self.subTest(obj=obj, field=field), self.assertRaises(SafetyError):
                plan_absolute(state, TARGET, 5)

    def test_homing_and_mesh_guard_during_horizontal_absolute_move(self):
        state = mock_state()
        prepared = plan_absolute(state, {'x': 110, 'y': 100, 'z': 5}, 5)
        state['toolhead']['homed_axes'] = 'xy'
        with self.assertRaisesRegex(SafetyError, 'Home all axes'):
            check_execution_state(state, prepared)
        state['toolhead']['homed_axes'] = 'xyz'
        state['bed_mesh']['profile_name'] = 'default'
        with self.assertRaisesRegex(SafetyError, 'inactive bed mesh'):
            check_execution_state(state, prepared)

    def test_no_op_is_validated_and_sends_no_script(self):
        printer = MockPrinter()
        target = {'x': 100, 'y': 100, 'z': 5}
        prepared = printer.request({'action': 'absolute-preview', 'target': target, 'speed_mm_s': 5})['plan']
        result = run_prepared(printer, [], 5, prepared)
        self.assertEqual(result['commands'], 0)
        self.assertEqual(printer.submitted_scripts, [])
        self.assertEqual(result['reported_position'], [100, 100, 5])

    def test_remote_schema_rejects_arbitrary_fields(self):
        request = {'action': 'absolute-preview', 'target': TARGET, 'speed_mm_s': 5}
        self.assertEqual(validate_request(request), 'absolute-preview')
        for extra in ({'script': 'G28'}, {'steps': []}, {'z_speed_mm_s': 2}):
            with self.subTest(extra=extra), self.assertRaises(SafetyError):
                validate_request(dict(request, **extra))
        with self.assertRaises(SafetyError):
            validate_request(dict(request, action='absolute-move'))


class AbsoluteCLITests(unittest.TestCase):
    def invoke(self, printer, arguments, confirmation=None):
        output, error = io.StringIO(), io.StringIO()
        with patch('simple_runner.SSHPrinter', return_value=printer), \
                patch('simple_runner.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value=confirmation) as prompt, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = run([], argv=['--real'] + arguments)
        return code, output.getvalue(), error.getvalue(), prompt

    def test_preview_has_no_mutations_or_confirmation(self):
        printer = MockPrinter()
        printer.simulated = False
        result = self.invoke(printer, ['--target', '110', '120', '15', '--speed', '5', '--preview'])
        self.assertEqual(result[0], 0, result[2])
        self.assertIn('one straight XYZ line', result[1])
        self.assertIn('Preview only; no motion sent', result[1])
        result[3].assert_not_called()
        self.assertEqual(printer.submitted_scripts, [])

    def test_execute_requires_move_and_reports_completion(self):
        printer = MockPrinter()
        printer.simulated = False
        arguments = ['--target', '110', '120', '15', '--speed', '5']
        rejected = self.invoke(printer, arguments, 'cancel')
        self.assertEqual(rejected[0], 1)
        self.assertEqual(printer.submitted_scripts, [])
        accepted = self.invoke(printer, arguments, 'MOVE')
        self.assertEqual(accepted[0], 0, accepted[2])
        self.assertIn('Moving...', accepted[1])
        self.assertIn('Completed.', accepted[1])
        self.assertEqual(len(printer.submitted_scripts), 1)

    def test_absolute_move_does_not_offer_implicit_mesh_clear(self):
        printer = MockPrinter()
        printer.simulated = False
        printer.state['bed_mesh']['profile_name'] = 'default'
        with patch.object(printer, 'request', wraps=printer.request) as requests:
            result = self.invoke(printer, ['--target', '110', '120', '15'])
        self.assertEqual(result[0], 1)
        self.assertIn('mesh-clear', result[2])
        self.assertEqual([c.args[0]['action'] for c in requests.call_args_list], ['status'])
        result[3].assert_not_called()

    def test_invalid_target_rejected_before_connection(self):
        with patch('simple_runner.SSHPrinter', side_effect=AssertionError('SSH accessed')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run([], argv=['--target', 'inf', '100', '10']), 1)
            self.assertEqual(run([], argv=['--target', 'nan', '100', '10']), 1)

    def test_missing_target_and_incompatible_actions(self):
        for arguments in (['--target', '1', '2'], ['--target', '1', '2', '3', '--status'],
                          ['--target', '1', '2', '3', '--z-speed', '2']):
            with self.subTest(arguments=arguments), patch('simple_runner.SSHPrinter') as connection, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                run([], argv=arguments)
            self.assertEqual(error.exception.code, 2)
            connection.assert_not_called()

    def test_no_op_needs_no_confirmation(self):
        printer = MockPrinter()
        printer.simulated = False
        result = self.invoke(printer, ['--target', '100', '100', '5'])
        self.assertEqual(result[0], 0)
        self.assertIn('Already at target', result[1])
        result[3].assert_not_called()
        self.assertEqual(printer.submitted_scripts, [])

    def test_printerctl_move_command_from_another_directory(self):
        command = Path(__file__).resolve().parents[1] / 'printerctl'
        result = subprocess.run([str(command), 'move', '110', '120', '15', '--speed', '5', '--mock'],
                                cwd='/tmp', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Completed (simulation). Reported position: [110.0, 120.0, 15.0]', result.stdout)


class AbsoluteWatchdogTests(unittest.TestCase):
    def request(self):
        return {'action': 'absolute-move', 'target': TARGET, 'speed_mm_s': 5,
                'expected_start': [100, 100, 5]}

    def test_changed_start_and_lost_homing_prevent_dispatch(self):
        for mutation in ('position', 'homing'):
            api, stop = FakeAPI(), Mock()
            request = self.request()
            if mutation == 'position':
                request['expected_start'] = [99, 100, 5]
            else:
                api.state['toolhead']['homed_axes'] = 'xy'
            with self.subTest(mutation=mutation), self.assertRaises(SafetyError):
                execute(api, {}, request, stop, 0)
            self.assertEqual(api.calls, [])
            stop.assert_not_called()

    def test_no_op_sends_no_gcode(self):
        api, stop = FakeAPI(), Mock()
        request = dict(self.request(), target={'x': 100, 'y': 100, 'z': 5})
        result = execute(api, {}, request, stop, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['commands'], 0)
        self.assertEqual(api.calls, [])
        stop.assert_not_called()

    def test_absolute_transport_failure_keeps_no_retry_stop_behavior(self):
        prepared = plan_absolute(mock_state(), TARGET, 5)
        printer = Mock()
        printer.request.side_effect = [PrinterError('SSH lost'), {'ok': True, 'stop_confirmed': True}]
        with self.assertRaises(PrinterError):
            run_prepared(printer, [], 5, prepared)
        self.assertEqual([c.args[0]['action'] for c in printer.request.call_args_list], ['absolute-move', 'stop'])

    def test_planned_z_range_and_speed_faults_request_stop(self):
        for field, value in (('live_position', [110, 120, 16]), ('live_velocity', 5.02)):
            def mutate(state):
                state['motion_report'][field] = value
            api, stop = FakeAPI(mutate), Mock(return_value=True)
            with self.subTest(field=field):
                result = execute(api, {}, self.request(), stop, 0)
                self.assertFalse(result['ok'])
                self.assertTrue(result['stop_confirmed'])
                self.assertEqual(len(api.calls), 1)
                stop.assert_called_once()
