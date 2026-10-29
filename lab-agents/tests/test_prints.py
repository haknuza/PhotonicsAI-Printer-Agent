"""Dashboard final-print controls; scripted fixtures only, no printer I/O."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import create_app, main
from printing_agent.agent import PrintingAgent
from printing_agent.openscad import ModelJobs
from printing_agent.print_jobs import PrintJobs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'printing_agent/tests'))
from test_openscad import FixtureAdapter, wait_for_job
from test_print_jobs import FixtureSlicer, FixtureTransport, wait_print

HEADERS = {'X-Lab-Control': 'local'}


class PrintAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models = ModelJobs(Path(self.temp.name) / 'models', adapter=FixtureAdapter())
        self.addCleanup(self.models.close)
        self.transport = FixtureTransport()
        self.prints = PrintJobs(Path(self.temp.name) / 'prints', self.models, self.transport, FixtureSlicer())
        self.addCleanup(self.prints.close)
        self.agent = PrintingAgent()
        self.addCleanup(self.agent.close)
        self.client = create_app(self.agent, models=self.models, prints=self.prints).test_client()
        job = self.models.submit('cube([20,10,5]);')
        self.model = wait_for_job(self.models, job['job_id'])

    def prepared(self):
        model = self.model
        self.models.approve(model['job_id'], model['source_sha256'], model['stl_sha256'], True)
        response = self.client.post('/api/prints', headers=HEADERS,
                                    json={'model_job_id': model['job_id'], 'profile_id': 'ender3v3ke-pla-04'})
        self.assertEqual(response.status_code, 202, response.get_json())
        return wait_print(self.prints, response.get_json()['job_id'])

    def test_prepare_review_approve_and_explicit_start_are_separate(self):
        job = self.prepared()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.client.get(job['artifact']).status_code, 200)
        self.assertEqual(self.client.get('/api/prints').get_json()['jobs'][0]['status'], 'ready')
        response = self.client.post('/api/prints/' + job['job_id'] + '/approve', headers=HEADERS,
                                    json={'gcode_sha256': job['gcode_sha256'], 'confirmed': True, 'setup_confirmed': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transport.calls, [])
        start = self.client.post('/api/prints/' + job['job_id'] + '/start', headers=HEADERS,
                                 json={'token': response.get_json()['token']})
        self.assertEqual(start.status_code, 202)
        self.assertEqual(start.get_json()['status'], 'submitted')
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_print_controls_require_same_origin_header_and_closed_fields(self):
        payload = {'model_job_id': self.model['job_id'], 'profile_id': 'ender3v3ke-pla-04'}
        self.assertEqual(self.client.post('/api/prints', json=payload).status_code, 403)
        self.assertEqual(self.client.post('/api/prints', json=payload,
                         headers={**HEADERS, 'Origin': 'https://other.invalid'}).status_code, 403)
        for extra in ('gcode', 'printer_host', 'approved', 'token', 'source'):
            self.assertEqual(self.client.post('/api/prints', headers=HEADERS,
                             json=dict(payload, **{extra: 'untrusted'})).status_code, 400)
        self.assertEqual(self.transport.calls, [])

    def test_disabled_default_does_not_select_hardware(self):
        client = create_app(self.agent, models=self.models).test_client()
        self.assertEqual(client.get('/api/config').get_json()['printing'], 'disabled')
        self.assertEqual(client.post('/api/prints', json={}, headers=HEADERS).status_code, 400)
        with patch('app.prepare_startup'), patch('app.PrintingAgent') as constructor, patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                main(['--print-backend', 'ssh'])
            constructor.assert_not_called()
