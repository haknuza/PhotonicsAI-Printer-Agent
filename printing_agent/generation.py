"""Bounded description → provider → validated source → existing compile jobs."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .openscad import JOB_ID, MAX_SOURCE_BYTES, ModelError, validate_source
from .providers import MAX_DESCRIPTION_BYTES, create_provider, validate_proposal


class GenerationJobs:
    def __init__(self, models, provider=None, workers=2, capacity=4, max_requests=100):
        self.models = models
        self.root = models.root / 'generations'
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='model-generation')
        self.slots = threading.BoundedSemaphore(capacity)
        self.requests, self.closed = {}, False
        self.max_requests = max_requests
        self.configuration_error = None
        try:
            self.provider = provider if provider is not None else create_provider()
        except ModelError as exc:
            self.provider, self.configuration_error = None, exc
        self._restore()

    def configuration(self):
        error = self.configuration_error or self.models.configuration_error
        return dict(available=error is None,
                    provider=self.provider.name if self.provider else None,
                    model=self.provider.model if self.provider else None,
                    error={'code': error.code, 'message': str(error)} if error else None,
                    description_limit_bytes=MAX_DESCRIPTION_BYTES,
                    automatic_retry=False, sends_source_to_provider=True)

    def _save(self, request):
        path = self.root / (request['request_id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(request, allow_nan=False))
        temporary.replace(path)

    def _restore(self):
        for path in self.root.glob('*.json'):
            if not JOB_ID.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    continue
                request = json.loads(path.read_text())
                if request['request_id'] != path.stem:
                    continue
                if request['status'] in ('queued', 'generating'):
                    request.update(status='error', error={'code':'INTERRUPTED', 'message':'Generation was interrupted. Submit again explicitly; it was not retried.'})
                    self._save(request)
                self.requests[path.stem] = request
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _lookup(self, request_id):
        if not isinstance(request_id, str) or not JOB_ID.fullmatch(request_id) or request_id not in self.requests:
            raise ModelError('GENERATION_NOT_FOUND', 'Unknown generation request ID.', 404)
        return self.requests[request_id]

    def _public(self, request):
        reply = {key: copy.deepcopy(value) for key, value in request.items() if key != 'base_source'}
        # Jobs already own a source artifact; drafts without a job remain recoverable.
        if request.get('job_id'):
            reply.pop('generated_source', None)
            job = self.models.get(request['job_id'])
            reply['status'] = 'compiling' if job['status'] in ('queued','compiling') else job['status']
            reply['job'] = job
            if job['error']:
                reply['error'] = job['error']
        return reply

    def get(self, request_id):
        with self.lock:
            return self._public(self._lookup(request_id))

    def list(self):
        with self.lock:
            return [self._public(request) for request in sorted(self.requests.values(), key=lambda value:value['created_at'], reverse=True)]

    def submit(self, description, parent_job_id=None, previous_request_id=None, base_source=None):
        if not isinstance(description, str) or not description.strip():
            raise ModelError('INVALID_DESCRIPTION', 'Describe the object or answer the design questions.')
        try:
            size = len(description.encode('utf-8'))
        except UnicodeError:
            size = MAX_DESCRIPTION_BYTES + 1
        if size > MAX_DESCRIPTION_BYTES or '\0' in description:
            raise ModelError('INVALID_DESCRIPTION', 'Descriptions must be valid UTF-8, at most 4000 bytes, without NULs.')
        with self.lock:
            if self.closed:
                raise ModelError('SERVICE_CLOSED', 'The generation service is shutting down.', 503)
            if self.configuration_error:
                raise self.configuration_error
            if self.models.configuration_error:
                raise self.models.configuration_error
            previous = self._lookup(previous_request_id) if previous_request_id is not None else None
            if previous:
                if previous['status'] != 'needs_clarification' or previous.get('continued_by'):
                    raise ModelError('CONVERSATION_CLOSED', 'This clarification has already been answered or is no longer pending.', 409)
                if parent_job_id is not None or base_source is not None:
                    raise ModelError('INVALID_CONTEXT', 'A clarification reply reuses its server-owned source and parent context.')
                turns = previous['turns'] + [{'role':'assistant','content':json.dumps(previous['proposal'])}]
                parent_job_id, source = previous['parent_job_id'], previous['base_source']
            else:
                turns, source = [], ''
                if parent_job_id is not None:
                    parent = self.models.get(parent_job_id)
                    if not parent['is_latest']:
                        raise ModelError('CONTEXT_CHANGED', 'Select the current model revision before requesting an AI revision.', 409)
                    source = self.models.artifact(parent_job_id,'model.scad').read_text()
                    if parent['error']:
                        diagnostic = dict(error=parent['error'], stderr=parent['stderr'][:8000], stdout=parent['stdout'][:2000])
                        turns.append({'role':'assistant', 'content':json.dumps({'previous_compilation':diagnostic})})
                if base_source is not None:
                    if base_source != '':
                        validate_source(base_source)
                    elif not isinstance(base_source, str):
                        raise ModelError('INVALID_SOURCE', 'Base source must be text.')
                    source = base_source
            if parent_job_id is not None and not self.models.get(parent_job_id)['is_latest']:
                raise ModelError('CONTEXT_CHANGED', 'The model changed during clarification; select the latest revision before continuing.', 409)
            if sum(turn['role'] == 'user' for turn in turns) >= 6:
                raise ModelError('CONTEXT_LIMIT', 'This design conversation reached six turns; start a new request.', 409)
            turns = turns + [{'role':'user','content':description.strip()}]
            if len(self.requests) >= self.max_requests or not self.slots.acquire(blocking=False):
                raise ModelError('GENERATION_QUEUE_FULL', 'Generation capacity reached; wait or archive requests with the backend stopped.', 429)
            request_id = uuid.uuid4().hex
            request = dict(request_id=request_id, created_at=time.time(), status='queued',
                           provider=self.provider.name, model=self.provider.model, description=description.strip(),
                           turns=turns, base_source=source, parent_job_id=parent_job_id,
                           previous_request_id=previous_request_id, continued_by=None,
                           proposal=None, generated_source=None, job_id=None, error=None)
            try:
                self._save(request)
                self.requests[request_id] = request
                if previous:
                    previous['continued_by'] = request_id
                    self._save(previous)
                self.pool.submit(self._generate, request_id)
            except BaseException:
                self.slots.release()
                raise
            return self._public(request)

    def _generate(self, request_id):
        try:
            with self.lock:
                request = self.requests[request_id]
                request['status'] = 'generating'
                self._save(request)
                turns, source = copy.deepcopy(request['turns']), request['base_source']
            proposal = validate_proposal(self.provider.generate(turns, source))
            public_proposal = {key:value for key,value in proposal.items() if key != 'source'}
            with self.lock:
                request['proposal'] = public_proposal
                if proposal['outcome'] == 'ready':
                    # Preserve paid-for, validated source even if the compiler queue
                    # fills or a concurrent edit makes the compilation context stale.
                    request['generated_source'] = proposal['source']
                    self._save(request)
                    provenance = dict(origin='ai-generated', provider=request['provider'], model=request['model'],
                                      request_id=request_id, description=request['description'],
                                      title=proposal['title'], explanation=proposal['explanation'],
                                      assumptions=proposal['assumptions'],
                                      base_source_sha256=hashlib.sha256(source.encode()).hexdigest() if source else None)
                    job = self.models.submit(proposal['source'], 'generated_model.scad', request['parent_job_id'],
                                             provenance=provenance, require_current_parent=True)
                    request.update(status='compiling', job_id=job['job_id'])
                else:
                    request['status'] = proposal['outcome']
                self._save(request)
        except Exception as exc:
            with self.lock:
                # Unexpected exceptions must not echo provider keys, raw replies or source.
                error = exc if isinstance(exc, ModelError) else ModelError('GENERATION_FAILED', 'Generation failed. No automatic retry was made.')
                request.update(status='error', error={'code':error.code, 'message':str(error),
                                                     'retry_after_seconds':getattr(error,'retry_after_seconds',None)})
                self._save(request)
        finally:
            self.slots.release()

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True)
