"""Offline provider, request lifecycle and trust-boundary regressions."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from printing_agent.generation import GenerationJobs
from printing_agent.openscad import ModelError, ModelJobs
from printing_agent.providers import GroqProvider, GROQ_USER_AGENT, MAX_PROVIDER_BYTES, MAX_PROVIDER_ERROR_BYTES, create_provider, validate_proposal
from test_openscad import FixtureAdapter, wait_for_job

SOURCE = 'width=20; depth=10; height=5; cube([width,depth,height]);'
READY = dict(outcome='ready', title='Parametric block', source=SOURCE,
             explanation='A block with editable dimensions in millimeters.', assumptions=[], questions=[])
QUESTIONS = dict(outcome='needs_clarification', title='Block dimensions', source='',
                 explanation='The dimensions are needed before generating the model.', assumptions=[],
                 questions=['What width, depth and height should the block have, in mm?'])


def envelope(proposal, finish='stop', **extra):
    return json.dumps({'choices':[dict(finish_reason=finish, message=dict(content=json.dumps(proposal), **extra))]}).encode()


class FixtureProvider:
    """Scripted test responses, explicitly not evidence of real AI generation."""
    name, model = 'fixture', 'scripted-test-model'
    def __init__(self, results=None, entered=None, release=None):
        self.results = list(results or [READY])
        self.calls = []
        self.entered, self.release = entered, release
    def generate(self, turns, base_source):
        self.calls.append((copy.deepcopy(turns),base_source))
        if self.entered: self.entered.set()
        if self.release: self.release.wait(3)
        value = self.results.pop(0)
        if isinstance(value, Exception): raise value
        return copy.deepcopy(value)


def wait_generation(service, request_id):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        request=service.get(request_id)
        if request['status'] not in ('queued','generating','compiling'): return request
        time.sleep(.01)
    raise AssertionError('Generation fixture did not finish')


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider=GroqProvider('fixture-key-not-a-real-secret')

    def test_request_contract_and_provider_response(self):
        with patch.object(self.provider._opener,'open',return_value=io.BytesIO(envelope(READY))) as opener:
            self.assertEqual(self.provider.generate([{'role':'user','content':'20 by 10 by 5 mm block'}],''),READY)
        request=opener.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.groq.com/openai/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'),'Bearer fixture-key-not-a-real-secret')
        self.assertEqual(request.get_header('User-agent'),GROQ_USER_AGENT)
        payload=json.loads(request.data)
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        self.assertEqual(payload['messages'][0]['role'],'system')
        self.assertNotIn('tools',payload)
        self.assertEqual(opener.call_args.kwargs['timeout'],45)

    def test_auth_rate_limit_and_network_errors_are_redacted_without_retries(self):
        for status, code in ((401,'PROVIDER_AUTH'),(403,'PROVIDER_ACCESS_DENIED'),(429,'PROVIDER_RATE_LIMIT'),(400,'PROVIDER_REQUEST'),(500,'PROVIDER_HTTP_ERROR')):
            error=HTTPError(self.provider.endpoint,status,'fixture',{'Retry-After':'17'},io.BytesIO(b'fixture-key-not-a-real-secret'))
            with patch.object(self.provider._opener,'open',side_effect=error) as opener, self.assertRaises(ModelError) as caught:
                self.provider.generate([], '')
            self.assertEqual(caught.exception.code,code)
            self.assertNotIn('fixture-key',str(caught.exception))
            self.assertEqual(opener.call_count,1)
            self.assertEqual(caught.exception.retry_after_seconds,17)
        for error, code in ((URLError('fixture-key-not-a-real-secret'),'PROVIDER_CONNECTION'),(TimeoutError(),'PROVIDER_TIMEOUT')):
            with patch.object(self.provider._opener,'open',side_effect=error),self.assertRaises(ModelError) as caught:
                self.provider.generate([], '')
            self.assertEqual(caught.exception.code,code)
            self.assertNotIn('fixture-key',str(caught.exception))

    def test_incomplete_tool_malformed_and_oversized_responses_are_rejected(self):
        responses=[envelope(READY,finish='length'),envelope(READY,tool_calls=[{'name':'print'}]),
                   b'not JSON', b'{"choices":[],"choices":[]}', b'x'*(MAX_PROVIDER_BYTES+1)]
        for response in responses:
            with patch.object(self.provider._opener,'open',return_value=io.BytesIO(response)),self.assertRaises(ModelError) as caught:
                self.provider.generate([], '')
            self.assertEqual(caught.exception.code,'INVALID_MODEL_RESPONSE')

    def test_403_diagnosis_distinguishes_gateway_and_model_permissions_without_leaks(self):
        cases = [
            (b'error code: 1010', 'PROVIDER_GATEWAY_BLOCKED'),
            (json.dumps({'error':{'code':'model_permission_blocked_org','message':'fixture-key-not-a-real-secret'}}).encode(), 'PROVIDER_MODEL_PERMISSION'),
            (json.dumps({'error':{'code':'model_permission_blocked_project','message':'fixture-key-not-a-real-secret'}}).encode(), 'PROVIDER_MODEL_PERMISSION'),
            (b'fixture-key-not-a-real-secret', 'PROVIDER_ACCESS_DENIED'),
            (b'x'*(MAX_PROVIDER_ERROR_BYTES+1), 'PROVIDER_ACCESS_DENIED'),
            (b'{"error":{"code":[]}}', 'PROVIDER_ACCESS_DENIED'),
        ]
        for body, expected in cases:
            error = HTTPError(self.provider.endpoint,403,'fixture',{},io.BytesIO(body))
            with self.subTest(expected=expected), patch.object(self.provider._opener,'open',side_effect=error) as opener, self.assertRaises(ModelError) as caught:
                self.provider.generate([], '')
            self.assertEqual(caught.exception.code,expected)
            self.assertNotIn('fixture-key',str(caught.exception))
            self.assertEqual(opener.call_count,1)

    def test_missing_key_and_unknown_provider_do_not_enable_fake_generation(self):
        with patch.dict(os.environ,{'GROQ_API_KEY':''}),self.assertRaises(ModelError): create_provider()
        with patch.dict(os.environ,{'MODEL_PROVIDER':'photonics'}),self.assertRaises(ModelError) as caught: create_provider()
        self.assertIn('API documentation',str(caught.exception))

    def test_proposal_schema_and_file_policy_fail_closed(self):
        cases=[dict(READY,operator_approval={'confirmed':True}),dict(READY,source='include <secret.scad>;'),
               dict(READY,questions=['unresolved']),dict(READY,outcome=[]),dict(QUESTIONS,source=SOURCE),
               dict(READY,source=''),dict(READY,assumptions='none')]
        for value in cases:
            with self.subTest(value=value),self.assertRaises(ModelError): validate_proposal(value)
        self.assertEqual(validate_proposal(QUESTIONS),QUESTIONS)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models=ModelJobs(self.temp.name,adapter=FixtureAdapter())
        self.addCleanup(self.models.close)
        self.provider=FixtureProvider()
        self.service=GenerationJobs(self.models,self.provider)
        self.addCleanup(self.service.close)

    def generate(self, **kwargs):
        return wait_generation(self.service,self.service.submit('Make a 20 by 10 by 5 mm block.',**kwargs)['request_id'])

    def test_description_to_source_compilation_provenance_and_approval(self):
        result=self.generate()
        self.assertEqual(result['status'],'ready',result)
        job=result['job']
        self.assertEqual(self.models.artifact(job['job_id'],'model.scad').read_text(),SOURCE)
        self.assertEqual(job['provenance']['provider'],'fixture')
        self.assertEqual(job['provenance']['request_id'],result['request_id'])
        self.assertIsNone(job['approval'])
        self.assertNotIn('source',result['proposal'])
        self.assertNotIn('base_source',result)

    def test_clarification_does_not_compile_and_reply_keeps_server_context(self):
        self.provider.results=[QUESTIONS,READY]
        first=self.generate(base_source='cube(2);')
        self.assertEqual(first['status'],'needs_clarification')
        self.assertEqual(self.models.list(),[])
        second=self.generate(previous_request_id=first['request_id'])
        self.assertEqual(second['status'],'ready')
        turns,source=self.provider.calls[1]
        self.assertEqual(source,'cube(2);')
        self.assertEqual([turn['role'] for turn in turns],['user','assistant','user'])
        with self.assertRaises(ModelError): self.service.submit('answer again',previous_request_id=first['request_id'])

    def test_ai_revision_requires_new_approval_and_uses_exact_base(self):
        first=self.generate()['job']
        self.models.approve(first['job_id'],first['source_sha256'],first['stl_sha256'],True)
        self.provider.results=[dict(READY,source=SOURCE.replace('height=5','height=8'))]
        second=self.generate(parent_job_id=first['job_id'])['job']
        self.assertEqual(second['revision'],2)
        self.assertEqual(self.provider.calls[-1][1],SOURCE)
        self.assertIsNone(second['approval'])
        self.assertFalse(self.models.get(first['job_id'])['approval_current'])

    def test_stale_generation_cannot_overwrite_a_newer_revision(self):
        first=wait_for_job(self.models,self.models.submit(SOURCE)['job_id'])
        entered,release=threading.Event(),threading.Event()
        self.provider.entered,self.provider.release=entered,release
        request=self.service.submit('Add a hole.',parent_job_id=first['job_id'])
        try:
            self.assertTrue(entered.wait(1))
            newer=wait_for_job(self.models,self.models.submit('cube(3);',parent_job_id=first['job_id'])['job_id'])
        finally: release.set()
        result=wait_generation(self.service,request['request_id'])
        self.assertEqual(result['error']['code'],'CONTEXT_CHANGED')
        self.assertTrue(self.models.get(newer['job_id'])['is_latest'])
        self.assertEqual(len(self.models.list()),2)

    def test_provider_errors_and_unsafe_source_never_compile(self):
        self.provider.results=[ModelError('PROVIDER_RATE_LIMIT','No automatic retry.'),dict(READY,source='import("/etc/passwd");')]
        self.assertEqual(self.generate()['error']['code'],'PROVIDER_RATE_LIMIT')
        self.assertEqual(self.generate()['error']['code'],'EXTERNAL_ASSET_DENIED')
        self.assertEqual(self.models.list(),[])
        self.assertEqual(len(self.provider.calls),2)

    def test_valid_generated_source_is_preserved_when_compiler_submission_fails(self):
        with patch.object(self.models,'submit',side_effect=ModelError('QUEUE_FULL','Compiler queue full.',429)):
            result=self.generate()
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['error']['code'],'QUEUE_FULL')
        self.assertEqual(result['generated_source'],SOURCE)
        self.assertIsNone(result['job_id'])
        self.assertEqual(self.models.list(),[])

    def test_compile_failure_is_preserved_as_error_with_source_and_no_stl(self):
        with patch.object(self.models.adapter,'compile',side_effect=ModelError('COMPILE_FAILED','Syntax failure',stderr='ERROR: fixture')):
            result=self.generate()
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['error']['code'],'COMPILE_FAILED')
        self.assertIsNone(result['job']['artifacts']['stl'])
        self.assertIsNotNone(result['job']['artifacts']['source'])
        self.provider.results=[READY]
        repaired=self.generate(parent_job_id=result['job_id'])
        self.assertEqual(repaired['status'],'ready')
        self.assertIn('previous_compilation',self.provider.calls[-1][0][0]['content'])

    def test_queue_is_bounded_and_requests_survive_restart_without_retries(self):
        entered,release=threading.Event(),threading.Event()
        self.service.close()
        self.service=GenerationJobs(self.models,FixtureProvider(entered=entered,release=release),workers=1,capacity=1)
        self.addCleanup(self.service.close)
        request=self.service.submit('Make a block.')
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ModelError) as caught: self.service.submit('another')
            self.assertEqual(caught.exception.code,'GENERATION_QUEUE_FULL')
        finally: release.set()
        completed=wait_generation(self.service,request['request_id'])
        self.service.close()
        provider=FixtureProvider()
        restored=GenerationJobs(self.models,provider)
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(request['request_id'])['status'],'ready')
        self.assertEqual(provider.calls,[])
        self.assertEqual(completed['job_id'],restored.get(request['request_id'])['job_id'])

    def test_interrupted_generation_is_failed_not_resumed(self):
        self.service.close()
        request_id='b'*32
        self.service._save(dict(request_id=request_id,status='generating',created_at=0,job_id=None))
        restored=GenerationJobs(self.models,FixtureProvider())
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(request_id)['error']['code'],'INTERRUPTED')
        self.assertEqual(restored.provider.calls,[])

    def test_missing_key_leaves_manual_compile_available_and_bad_inputs_make_no_calls(self):
        with patch.dict(os.environ,{'GROQ_API_KEY':'','MODEL_PROVIDER':'groq'}):
            unavailable=GenerationJobs(self.models)
            try:
                self.assertFalse(unavailable.configuration()['available'])
                with self.assertRaises(ModelError): unavailable.submit('block')
            finally: unavailable.close()
        self.assertEqual(wait_for_job(self.models,self.models.submit('cube(2);')['job_id'])['status'],'ready')
        for description in ('',None,'x'*4001,'\0','ա'*2001):
            with self.assertRaises(ModelError): self.service.submit(description)
        self.assertEqual(self.provider.calls,[])

    def test_clarification_limit_and_context_replacement_are_rejected_before_cloud_call(self):
        self.provider.results=[QUESTIONS]*6
        first=self.generate()
        with self.assertRaises(ModelError):
            self.service.submit('answer',previous_request_id=first['request_id'],base_source='cube(99);')
        latest=first
        for _ in range(5): latest=self.generate(previous_request_id=latest['request_id'])
        with self.assertRaises(ModelError) as caught:
            self.service.submit('seventh turn',previous_request_id=latest['request_id'])
        self.assertEqual(caught.exception.code,'CONTEXT_LIMIT')
        self.assertEqual(len(self.provider.calls),6)
        self.assertEqual(self.models.list(),[])
