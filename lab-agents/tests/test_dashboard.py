"""Read-only mock dashboard boundaries; no devices or external services."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import create_app, main
from printing_agent.agent import PrintingAgent


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.agent = PrintingAgent()
        self.addCleanup(self.agent.close)
        self.client = create_app(self.agent).test_client()

    def test_reads_are_simulated_and_do_not_dispatch_motion(self):
        config = self.client.get('/api/config').get_json()
        self.assertEqual(config['printer_mode'], 'mock')
        self.assertTrue(config['printer_read_only'])
        self.assertEqual(config['printing'], 'disabled')
        for command in ('describe', 'status', 'position', 'capabilities'):
            with self.subTest(command=command):
                response = self.client.get('/api/printer/' + command)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()['simulated'])
                self.assertTrue(response.get_json()['ok'])
        self.assertEqual(self.agent._printer.submitted_scripts, [])
        self.assertEqual(self.agent._commands.tokens, {})

    def test_motion_and_legacy_controls_are_unavailable(self):
        for command in ('move', 'home', 'stop', 'print', 'gcode'):
            self.assertEqual(self.client.get('/api/printer/' + command).status_code, 404)
        for path in ('/api/prepare', '/api/stop', '/api/simulation/reset',
                     '/api/runs/anything/execute', '/api/runs/anything/image'):
            self.assertEqual(self.client.post(path, json={}).status_code, 404)
        self.assertEqual(self.client.post('/api/printer/status', json={}).status_code, 405)
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_template_assets_and_honest_scope(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'PhotonicsAI Printer Agent', response.data)
        self.assertIn(b'not implemented', response.data)
        self.assertIn(b'final print approval', response.data)
        for path in ('/static/ui.js', '/static/dashboard.css', '/static/photonicsai-logo.png'):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn(b'id="model-source"', response.data)
        self.assertNotIn(b'id="emergency"', response.data)
        self.assertNotIn(b'/api/prepare', response.data)

    def test_controller_error_is_unavailable_not_success(self):
        with patch.object(self.agent, 'handle', return_value=self.agent.failure('READ_FAILED', 'unavailable')):
            response = self.client.get('/api/printer/status')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()['ok'])

    def test_startup_cannot_select_hardware(self):
        with tempfile.TemporaryDirectory() as store, \
                patch.dict(os.environ, {'PRINTER_MODE': 'real', 'PRINTER_ALLOW_MOTION': '1', 'OPENSCAD_JOBS_DIR': store}), \
                patch('printer_client.SSHPrinter', side_effect=AssertionError('SSH accessed')), \
                patch('app.prepare_startup'), \
                patch('app.atexit.register') as on_exit, \
                patch('flask.Flask.run') as run:
            try:
                main([])
                run.assert_called_once()
            finally:
                for call in reversed(on_exit.call_args_list):
                    call.args[0]()
        with patch('app.PrintingAgent') as constructor, \
                patch('sys.stderr'), self.assertRaises(SystemExit) as rejected:
            main(['--printer-mode', 'real'])
        self.assertEqual(rejected.exception.code, 2)
        constructor.assert_not_called()
