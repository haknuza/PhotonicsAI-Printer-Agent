"""The connected printer's limits govern every movement interface."""
import copy
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from printer_client import MockPrinter, mock_state, run_prepared
from printer_commands import PrinterCommands
from printer_policy import SafetyError, check_execution_state, plan, plan_absolute, travel_limits
from printer_watchdog import execute
from simple_runner import run
from test_simple import FakeAPI
from test_xy import set_position


class FirmwareBoundsTests(unittest.TestCase):
    def test_actual_reported_ranges_replace_old_limits(self):
        state = mock_state()
        limits = {'x': [-12, 221], 'y': [-20, 223], 'z': [-5, 246]}
        self.assertEqual(travel_limits(state), limits)
        for target in ({'x': 210, 'y': 220, 'z': 245}, {'x': -11, 'y': -19, 'z': -4}):
            with self.subTest(target=target):
                result = plan_absolute(state, target, 20)
                self.assertEqual(result['end'], list(target.values()))
                self.assertEqual(result['firmware_limits_mm'], limits)

    def test_negative_homing_start_is_valid_for_normal_movement(self):
        state = mock_state()
        set_position(state, [-2, -14.9, 9.9])
        target = {'x': 0, 'y': 0, 'z': 30}
        self.assertEqual(plan_absolute(state, target, 5)['end'], [0, 0, 30])
        self.assertEqual(plan(state, [{'axis': 'y', 'distance_mm': 50}], 5)['end'], [-2, 35.1, 9.9])

    def test_limits_are_read_from_state_not_a_model_specific_constant(self):
        state = mock_state()
        state['toolhead']['axis_minimum'] = [-100, -110, -20, 0]
        state['toolhead']['axis_maximum'] = [400, 410, 500, 0]
        result = plan_absolute(state, {'x': 390, 'y': 405, 'z': 490}, 20)
        self.assertEqual(result['bounds'], [-100, 400, -110, 410])
        self.assertEqual(result['z_bounds'], [-20, 500])

    def test_missing_nonfinite_and_inverted_limits_fail_closed(self):
        for field, value in (('axis_minimum', None), ('axis_maximum', [221, 223]),
                             ('axis_minimum', [False, -20, -5]),
                             ('axis_maximum', [float('nan'), 223, 246]),
                             ('axis_maximum', [float('inf'), 223, 246]),
                             ('axis_minimum', [221, -20, -5])):
            state = mock_state()
            state['toolhead'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SafetyError):
                plan_absolute(state, {'x': 100, 'y': 120, 'z': 30}, 5)

    def test_each_firmware_edge_and_rounded_target_checked(self):
        for axis, value in (('x', -12.0001), ('x', 221.0001), ('y', -20.0001),
                            ('y', 223.0001), ('z', -5.0001), ('z', 246.0001)):
            target = {'x': 100, 'y': 100, 'z': 5}
            target[axis] = value
            with self.subTest(axis=axis, value=value), self.assertRaises(SafetyError):
                plan_absolute(mock_state(), target, 5)
        state = mock_state()
        state['toolhead']['axis_maximum'][0] = 220.9996
        with self.assertRaises(SafetyError):
            plan_absolute(state, {'x': 220.9996, 'y': 100, 'z': 5}, 5)
        with self.assertRaises(SafetyError):
            plan(state, [{'axis': 'x', 'distance_mm': 120.9996}], 5)

    def test_changed_limits_before_dispatch_prevent_motion(self):
        printer = MockPrinter()
        prepared = plan_absolute(printer.state, {'x': 110, 'y': 120, 'z': 15}, 5)
        printer.state['toolhead']['axis_maximum'][0] = 220
        with self.assertRaisesRegex(SafetyError, 'changed since preview'):
            run_prepared(printer, [], 5, prepared)
        self.assertEqual(printer.submitted_scripts, [])
        api, stop = FakeAPI(), Mock()
        request = {'action': 'absolute-move', 'target': {'x': 110, 'y': 120, 'z': 15},
                   'speed_mm_s': 5, 'expected_start': [100, 100, 5],
                   'expected_limits_mm': prepared['firmware_limits_mm']}
        api.state['toolhead']['axis_maximum'][0] = 220
        with self.assertRaisesRegex(SafetyError, 'changed since preview'):
            execute(api, {}, request, stop, 0)
        self.assertEqual(api.calls, [])
        stop.assert_not_called()

    def test_changed_limits_during_execution_request_stop(self):
        def mutate(state):
            state['toolhead']['axis_maximum'][0] = 220
        api, stop = FakeAPI(mutate), Mock(return_value=True)
        result = execute(api, {}, {'action': 'absolute-move', 'target': {'x': 110, 'y': 120, 'z': 15},
                         'speed_mm_s': 5, 'expected_start': [100, 100, 5]}, stop, 0)
        self.assertFalse(result['ok'])
        self.assertIn('limits changed during execution', result['error'])
        self.assertTrue(result['stop_confirmed'])
        stop.assert_called_once()

    def test_json_capabilities_position_and_preview_use_same_limits(self):
        printer = MockPrinter()
        controller = PrinterCommands(printer)
        self.addCleanup(controller.close)
        limits = travel_limits(printer.state)
        for command in ('capabilities', 'position', 'status'):
            response = controller.execute({'command': command})
            self.assertEqual(response['data']['travel_limits_mm'], limits)
            self.assertEqual(response['data']['travel_limits_source'], 'firmware')
        response = controller.execute({'command': 'move', 'target': {'x': 210, 'y': 220, 'z': 245}})
        self.assertTrue(response['ok'], response)
        self.assertEqual(response['data']['plan']['limits_mm'], limits)
        response['data']['plan']['limits_mm']['x'][0] = -999
        token = response['data']['token']
        self.assertEqual(controller.tokens[token]['plan']['firmware_limits_mm']['x'][0], -12)
        rejected = controller.execute({'command': 'move', 'target': {'x': 222, 'y': 100, 'z': 5}})
        self.assertEqual(rejected['error']['code'], 'OUT_OF_BOUNDS')
        self.assertEqual(printer.submitted_scripts, [])

    def test_terminal_preview_displays_reported_limits_from_negative_start(self):
        printer = MockPrinter()
        printer.simulated = False
        set_position(printer.state, [-2, -14.9, 9.9])
        output = io.StringIO()
        with patch('simple_runner.SSHPrinter', return_value=printer), \
                patch('builtins.input', side_effect=AssertionError('Confirmation requested')), \
                contextlib.redirect_stdout(output):
            result = run([], argv=['--real', '--target', '0', '0', '30', '--speed', '5', '--preview'])
        self.assertEqual(result, 0)
        self.assertIn('X -12..221, Y -20..223, Z -5..246 mm', output.getvalue())
        self.assertEqual(printer.submitted_scripts, [])
