"""Immutable sliced jobs, separate approval and one-shot physical submission."""
from concurrent.futures import ThreadPoolExecutor
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from .openscad import JOB_ID, ModelError
from .slicing import MAX_GCODE_BYTES, PROFILES, inspect_gcode, profile_info
from .creality import CrealityPrint


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PrintJobs:
    def __init__(self, root, models, transport=None, slicer=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store_lock = open(self.root / '.lock', 'a')
        try:
            fcntl.flock(self.store_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.store_lock.close()
            raise ModelError('PRINT_STORE_BUSY', 'Another backend owns this print store.', 503) from exc
        self.models, self.transport = models, transport
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='slicer')
        self.slots = threading.BoundedSemaphore(2)
        self.jobs, self.tokens, self.closed = {}, {}, False
        self.configuration_error = None
        try:
            self.slicer = slicer if slicer is not None else CrealityPrint()
        except ModelError as exc:
            self.slicer, self.configuration_error = None, exc
        for directory in self.root.iterdir():
            if not JOB_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                metadata = directory / 'metadata.json'
                if metadata.is_symlink() or metadata.stat().st_size > 8 * 1024 * 1024:
                    continue
                job = json.loads(metadata.read_text())
                if job['job_id'] != directory.name:
                    continue
                slicer_changed = job.get('slicer_engine') != (self.slicer.engine_id if self.slicer else 'creality-print')
                if job['status'] in ('queued', 'slicing'):
                    job.update(status='error', error={'code': 'INTERRUPTED', 'message': 'Slicing was interrupted. Prepare a new job.'})
                if job['status'] in ('dispatching', 'submitted', 'printing', 'paused'):
                    job.update(status='uncertain', error={'code': 'INSPECT_PRINTER', 'message': 'Check printer status before any further action. Start will not be replayed.'})
                if slicer_changed and job['status'] in ('ready', 'approved'):
                    job.update(status='error', error={'code': 'SLICER_CHANGED',
                        'message': 'This job used the previous slicer. Prepare a fresh Creality Print job.'})
                if job['status'] == 'approved':
                    job['status'] = 'ready'
                job['approval'] = None
                self.jobs[directory.name] = job
                self._save(job)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def configuration(self):
        error = self.configuration_error
        if not error and not self.transport:
            error = ModelError('PRINT_CONNECTION_DISABLED', 'Restart with --print-backend ssh and an explicit --printer-host to enable print preparation.', 503)
        return {'available': error is None, 'backend': 'ssh' if self.transport else 'disabled',
                'simulated': self.transport.simulated if self.transport else None,
                'error': {'code': error.code, 'message': str(error)} if error else None,
                'slicer': 'Creality Print',
                'profiles': [self.slicer.profile_for(key)[0] if self.slicer else profile_info(key) for key in PROFILES],
                'approval_lifetime_seconds': 120}

    def _lookup(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or job_id not in self.jobs:
            raise ModelError('PRINT_NOT_FOUND', 'Unknown print job.', 404)
        return self.jobs[job_id]

    def _save(self, job):
        path = self.root / job['job_id'] / 'metadata.json'
        temporary = path.with_suffix('.tmp')
        with open(temporary, 'w') as stream:
            json.dump(job, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def _model(self, job):
        model = self.models.get(job['model_job_id'])
        if not model['approval_current'] or model['status'] != 'ready':
            raise ModelError('MODEL_APPROVAL_REQUIRED', 'Approve the latest compiled model revision first.', 409)
        if job.get('stl_sha256') and job['stl_sha256'] != model['stl_sha256']:
            raise ModelError('MODEL_CHANGED', 'The approved STL changed. Prepare a new print job.', 409)
        self.models.artifact(model['job_id'], 'model.scad')
        path = self.models.artifact(model['job_id'], 'model.stl')
        data = path.read_bytes()
        if digest(data) != model['stl_sha256']:
            raise ModelError('MODEL_CHANGED', 'STL artifact changed during preparation.', 409)
        return model, data

    def _validate(self, job):
        self._model(job)
        profile, _ = self.slicer.profile_for(job['profile']['id']) if self.slicer else (None, None)
        if not self.transport:
            raise ModelError('PRINT_CONNECTION_DISABLED', 'Configure this job\'s print connection first.', 503)
        if profile != job['profile'] or job['target_id'] != self.transport.target_id:
            raise ModelError('PRINT_CONTEXT_CHANGED', 'Printer or profile changed. Prepare a new print job.', 409)
        self._inputs(job)
        path = self.root / job['job_id'] / 'output/print.gcode'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_GCODE_BYTES:
            raise ModelError('GCODE_CHANGED', 'Sliced artifact is missing or invalid.', 409)
        data = path.read_bytes()
        if digest(data) != job['gcode_sha256']:
            raise ModelError('GCODE_CHANGED', 'Sliced artifact changed. Prepare a new job.', 409)
        inspect_gcode(data, profile)
        return data

    def _inputs(self, job):
        directory = self.root / job['job_id']
        _, settings = self.slicer.profile_for(job['profile']['id'])
        for name in ('model.stl', 'profile.json', 'output'):
            path = directory / name
            if directory.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(directory):
                raise ModelError('PRINT_INPUT_CHANGED', 'Print input path changed. Prepare a new job.', 409)
        if digest((directory / 'model.stl').read_bytes()) != job['stl_sha256'] or (directory / 'profile.json').read_text() != settings:
            raise ModelError('PRINT_INPUT_CHANGED', 'Print input or settings changed. Prepare a new job.', 409)

    def printer_status(self):
        if not self.transport:
            raise ModelError('PRINT_CONNECTION_DISABLED', 'The print connection is disabled.', 503)
        return self.transport.status()

    def prepare(self, model_job_id, profile_id):
        if self.configuration_error:
            raise self.configuration_error
        profile, settings = self.slicer.profile_for(profile_id)
        if not self.transport:
            raise ModelError('PRINT_CONNECTION_DISABLED', 'Configure the explicit print connection first.', 503)
        with self.lock, self.models.lock:
            if self.closed or len(self.jobs) >= 100 or not self.slots.acquire(blocking=False):
                raise ModelError('PRINT_CAPACITY', 'Print preparation is busy or at capacity.', 429)
            try:
                model, data = self._model({'model_job_id': model_job_id})
                dimensions = model['dimensions_mm']
                if dimensions['x'] > 208 or dimensions['y'] > 208 or dimensions['z'] > 240:
                    raise ModelError('MODEL_TOO_LARGE', 'This profile accepts models up to 208 × 208 × 240 mm, allowing room for the skirt.', 409)
                printer = self.printer_status()
                if printer.get('ready_for_print') is not True:
                    raise ModelError('PRINTER_NOT_READY', printer.get('readiness_error') or 'Printer readiness could not be established.', 409)
                job_id = uuid.uuid4().hex
                directory = self.root / job_id
                directory.mkdir(mode=0o700)
                (directory / 'output').mkdir(mode=0o700)
                (directory / 'model.stl').write_bytes(data)
                (directory / 'profile.json').write_text(settings)
                job = {'job_id': job_id, 'model_job_id': model_job_id, 'model_revision': model['revision'],
                       'stl_sha256': model['stl_sha256'], 'profile': profile, 'status': 'queued',
                       'slicer_engine': self.slicer.engine_id,
                       'created_at': time.time(), 'target_id': self.transport.target_id,
                       'configuration_sha256': printer['configuration_sha256'],
                       'printer_identity': printer['printer_identity'], 'simulated': self.transport.simulated,
                       'gcode_sha256': None, 'summary': None, 'approval': None, 'error': None}
                self._save(job)
                self.jobs[job_id] = job
                self.pool.submit(self._slice, job_id)
                return self.get(job_id)
            except BaseException:
                self.slots.release()
                raise

    def _slice(self, job_id):
        try:
            with self.lock:
                job = self._lookup(job_id)
                job['status'] = 'slicing'
                self._save(job)
            directory = self.root / job_id
            self._inputs(job)
            self.slicer.slice(directory)
            self._inputs(job)
            path = directory / 'output/print.gcode'
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_GCODE_BYTES:
                raise ModelError('INVALID_GCODE', 'Slicer did not create a valid output file.', 409)
            data = path.read_bytes()
            summary = inspect_gcode(data, job['profile'])
            with self.lock, self.models.lock:
                self._model(job)
                job.update(status='ready', gcode_sha256=digest(data), summary=summary)
                self._save(job)
        except Exception as exc:
            error = exc if isinstance(exc, ModelError) else ModelError('SLICE_FAILED', 'Slicing failed; no print can start.', 409)
            with self.lock:
                job.update(status='error', error={'code': error.code, 'message': str(error)})
                job['diagnostics'] = {'stdout': error.stdout, 'stderr': error.stderr}
                self._save(job)
        finally:
            self.slots.release()

    def get(self, job_id):
        with self.lock:
            reply = copy.deepcopy(self._lookup(job_id))
            reply['artifact'] = '/api/prints/' + job_id + '/gcode' if reply['gcode_sha256'] else None
            return reply

    def list(self):
        with self.lock:
            result = []
            for job in sorted(self.jobs.values(), key=lambda value: value['created_at'], reverse=True):
                value = self.get(job['job_id'])
                if value['summary']:
                    value['summary'].pop('preview_segments', None)
                result.append(value)
            return result

    def artifact(self, job_id):
        with self.lock, self.models.lock:
            job = self._lookup(job_id)
            if not job['gcode_sha256']:
                raise ModelError('GCODE_NOT_READY', 'This job has no sliced artifact.', 409)
            self._validate(job)
            return self.root / job_id / 'output/print.gcode'

    def approve(self, job_id, gcode_sha256, confirmed, setup_confirmed):
        with self.lock, self.models.lock:
            job = self._lookup(job_id)
            if confirmed is not True or setup_confirmed is not True:
                raise ModelError('PRINT_CONFIRMATION_REQUIRED', 'Confirm the exact job, loaded material/nozzle and a clear build plate.', 409)
            if job['status'] not in ('ready', 'approved') or gcode_sha256 != job['gcode_sha256']:
                raise ModelError('PRINT_NOT_APPROVABLE', 'Review this prepared job and its exact hash before approval.', 409)
            self._validate(job)
            state = self.printer_status()
            if state.get('ready_for_print') is not True or state.get('configuration_sha256') != job['configuration_sha256']:
                raise ModelError('PRINTER_CHANGED', 'Printer readiness/configuration changed. Prepare a new job.', 409)
            token = secrets.token_urlsafe(32)
            for old in [key for key, value in self.tokens.items() if value['job_id'] == job_id or value['expires_at'] < time.time()]:
                del self.tokens[old]
            approval = {'job_id': job_id, 'gcode_sha256': gcode_sha256, 'approved_at': time.time(),
                        'expires_at': time.time() + 120, 'scope': 'exact-print-job', 'setup_confirmed': True}
            self.tokens[token] = approval
            job.update(status='approved', approval=approval)
            self._save(job)
            return dict(self.get(job_id), token=token)

    def start(self, job_id, token):
        # Hold model ownership across dispatch so revisions cannot be accepted
        # between checking the approved model and submitting its exact job.
        with self.lock, self.models.lock:
            job = self._lookup(job_id)
            approval = self.tokens.pop(token, None) if isinstance(token, str) else None
            if not approval or approval['job_id'] != job_id or approval['expires_at'] < time.time() or job['status'] != 'approved':
                raise ModelError('PRINT_APPROVAL_REQUIRED', 'Use a fresh single-use approval for this exact print job.', 409)
            data = self._validate(job)
            job.update(status='dispatching')
            self._save(job)  # Persist intent before network I/O; never replay on restart.
            try:
                result = self.transport.submit(job_id, data, job['configuration_sha256'])
                if result.get('submission') != 'accepted':
                    raise ModelError('SUBMISSION_UNCERTAIN', 'Start outcome is uncertain. Inspect the printer.', 409)
                job.update(status='submitted', filename=result['filename'], error=None)
            except Exception as exc:
                error = exc if isinstance(exc, ModelError) else ModelError('SUBMISSION_UNCERTAIN', 'Start outcome is uncertain. Inspect the printer; do not retry.', 503)
                job.update(status='error' if error.code in ('PRINTER_REJECTED', 'SSH_SESSION_REQUIRED') else 'uncertain',
                           error={'code': error.code, 'message': str(error)})
                self._save(job)
                raise error
            self._save(job)
            return self.get(job_id)

    def refresh(self, job_id):
        with self.lock:
            job = self._lookup(job_id)
            state = self.printer_status()
            expected = 'photonics_' + job_id + '.gcode'
            if Path(state['print_stats'].get('filename') or '').name == expected:
                phase = state['print_stats']['state']
                if phase in ('printing', 'paused', 'complete', 'cancelled', 'error'):
                    job['status'] = {'complete': 'completed'}.get(phase, phase)
                    self._save(job)
            result = self.get(job_id)
            result['printer'] = state
            return result

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True)
        self.tokens.clear()
        if not self.store_lock.closed:
            self.store_lock.close()
