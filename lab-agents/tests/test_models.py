"""Model HTTP boundaries and approval semantics; compiler fixtures, no printer I/O."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from printing_agent.agent import PrintingAgent
from printing_agent.openscad import ModelJobs

# Share the explicit fake-compiler fixture, not a simulated claim about OpenSCAD.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'printing_agent/tests'))
from test_openscad import FixtureAdapter, wait_for_job

HEADERS = {'X-Lab-Control': 'local'}


class ModelAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models = ModelJobs(self.temp.name, adapter=FixtureAdapter())
        self.addCleanup(self.models.close)
        self.printer = PrintingAgent()
        self.addCleanup(self.printer.close)
        self.client = create_app(self.printer, models=self.models).test_client()

    def submit(self, **extra):
        response = self.client.post('/api/models', json=dict(source='cube([20,10,5]);', **extra), headers=HEADERS)
        self.assertEqual(response.status_code,202,response.get_json())
        return wait_for_job(self.models,response.get_json()['job_id'])

    def test_submit_poll_download_revise_and_approve_never_calls_printer(self):
        with patch.object(self.printer, 'handle', side_effect=AssertionError('Model operation touched printer')):
            job = self.submit()
            status=self.client.get('/api/models/'+job['job_id'])
            self.assertEqual(status.get_json()['status'],'ready')
            self.assertEqual(self.client.get('/api/models').get_json()['jobs'][0]['job_id'],job['job_id'])
            source=self.client.get(job['artifacts']['source'])
            stl=self.client.get(job['artifacts']['stl'])
            self.assertEqual(source.data,b'cube([20,10,5]);')
            self.assertGreater(len(stl.data),84)
            self.assertIn('attachment',stl.headers['Content-Disposition'])
            approval=dict(source_sha256=job['source_sha256'],stl_sha256=job['stl_sha256'],confirmed=True)
            approved=self.client.post('/api/models/'+job['job_id']+'/approve',json=approval,headers=HEADERS)
            self.assertTrue(approved.get_json()['approval_current'])
            revision=self.submit(parent_job_id=job['job_id'])
            self.assertIsNone(revision['approval'])
            stale=self.client.post('/api/models/'+job['job_id']+'/approve',json=approval,headers=HEADERS)
            self.assertEqual(stale.status_code,409)
        self.assertEqual(self.printer._printer.submitted_scripts,[])

    def test_post_header_origin_auth_and_input_validation(self):
        self.assertEqual(self.client.post('/api/models',json={'source':'cube(1);'}).status_code,403)
        self.assertEqual(self.client.post('/api/models',json={'source':'cube(1);'},headers={**HEADERS,'Origin':'https://other.invalid'}).status_code,403)
        for body in ({},[],{'source':'cube(1);','job_id':'chosen-by-client'},
                     {'source':'cube(1);','parent_job_id':'../x'}, {'source':5},
                     {'source':'include <secret.scad>;'},{'source':'cube(1);','filename':'../x.scad'}):
            response=self.client.post('/api/models',json=body,headers=HEADERS)
            self.assertIn(response.status_code,(400,404))
        remote=self.client.post('/api/models',json={'source':'cube(1);'},headers=HEADERS,
                                environ_overrides={'REMOTE_ADDR':'192.0.2.30'})
        self.assertEqual(remote.status_code,503)

    def test_ids_and_artifacts_are_allowlisted_and_no_store(self):
        job=self.submit()
        for suffix in ('metadata.json','output','anything.stl'):
            self.assertEqual(self.client.get('/api/models/'+job['job_id']+'/artifacts/'+suffix).status_code,404)
        self.assertEqual(self.client.get('/api/models/unknown').status_code,404)
        response=self.client.get(job['artifacts']['stl'])
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')

    def test_example_and_local_viewer_assets_are_available(self):
        example=self.client.get('/api/models/example').get_json()
        self.assertIn('not AI-generated',example['origin'])
        self.assertIn('outer_diameter',example['source'])
        for path in ('models.js','model-viewer.js','vendor/three-r160/build/three.module.js',
                     'vendor/three-r160/examples/jsm/loaders/STLLoader.js',
                     'vendor/three-r160/examples/jsm/controls/OrbitControls.js'):
            self.assertEqual(self.client.get('/static/'+path).status_code,200)
        self.assertTrue(self.client.get('/api/models/config').get_json()['available'])


class GenerationAPITests(unittest.TestCase):
    def setUp(self):
        from printing_agent.generation import GenerationJobs
        from test_generation import FixtureProvider
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models=ModelJobs(self.temp.name,adapter=FixtureAdapter())
        self.addCleanup(self.models.close)
        self.provider=FixtureProvider()
        self.generations=GenerationJobs(self.models,self.provider)
        self.addCleanup(self.generations.close)
        self.printer=PrintingAgent()
        self.addCleanup(self.printer.close)
        self.client=create_app(self.printer,models=self.models,generations=self.generations).test_client()

    def test_description_to_model_routes_never_dispatch_printer_or_approve(self):
        from test_generation import wait_generation
        with patch.object(self.printer,'handle',side_effect=AssertionError('Printer dispatch')):
            response=self.client.post('/api/models/generate',json={'description':'20 by 10 by 5 mm block'},headers=HEADERS)
            self.assertEqual(response.status_code,202)
            result=wait_generation(self.generations,response.get_json()['request_id'])
            polled=self.client.get('/api/models/generations/'+result['request_id'])
            self.assertEqual(polled.get_json()['status'],'ready')
            self.assertIsNone(result['job']['approval'])
            self.assertEqual(self.client.get('/api/models/generations').get_json()['requests'][0]['request_id'],result['request_id'])
            self.assertTrue(self.client.get('/api/models/config').get_json()['generation']['available'])
            self.assertEqual(self.client.get('/api/config').get_json()['model_generation'],'available')
            self.assertEqual(self.client.get(result['job']['artifacts']['source']).status_code,200)
        self.assertEqual(self.printer._printer.submitted_scripts,[])

    def test_generation_post_requires_header_origin_and_closed_fields(self):
        payload={'description':'block'}
        self.assertEqual(self.client.post('/api/models/generate',json=payload).status_code,403)
        self.assertEqual(self.client.post('/api/models/generate',json=payload,headers={**HEADERS,'Origin':'https://other.invalid'}).status_code,403)
        for body in ({'description':'block','confirmed':True},{'description':'block','provider':'photonics'},
                     {'description':'block','api_key':'never-accept-browser-keys'},{'description':False}):
            self.assertEqual(self.client.post('/api/models/generate',json=body,headers=HEADERS).status_code,400)
        self.assertEqual(self.provider.calls,[])

    def test_missing_credentials_do_not_disable_compilation(self):
        from printing_agent.generation import GenerationJobs
        with patch.dict('os.environ',{'MODEL_PROVIDER':'groq','GROQ_API_KEY':''}):
            service=GenerationJobs(self.models)
            self.addCleanup(service.close)
            client=create_app(self.printer,models=self.models,generations=service).test_client()
            self.assertFalse(client.get('/api/models/config').get_json()['generation']['available'])
            self.assertEqual(client.post('/api/models/generate',json={'description':'block'},headers=HEADERS).status_code,503)
            self.assertEqual(client.post('/api/models',json={'source':'cube(2);'},headers=HEADERS).status_code,202)
