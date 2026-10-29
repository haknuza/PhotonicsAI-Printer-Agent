"""Linux-only, isolated OpenSCAD compilation. No printer dependencies.

An empty external-asset allowlist is enforced: include/use/import/surface and
legacy file-reading operations are rejected. Bubblewrap supplies the OS boundary;
working directories and the lexical policy alone are not sandboxes.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

MAX_SOURCE_BYTES = 128 * 1024
MAX_STL_BYTES = 32 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024
MAX_TRIANGLES = 200_000
JOB_ID = re.compile(r'[a-f0-9]{32}\Z')
FILE_OPERATIONS = {'include', 'use', 'import', 'surface', 'import_stl', 'import_off',
                   'import_dxf', 'dxf_dim', 'dxf_cross'}
IDENTIFIER = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')


class ModelError(ValueError):
    def __init__(self, code, message, http_status=400, stdout='', stderr=''):
        super().__init__(message)
        self.code, self.http_status = code, http_status
        self.stdout, self.stderr = stdout, stderr


def locate_openscad(setting=None):
    configured = os.environ.get('OPENSCAD_BIN', '') if setting is None else setting
    candidate = shutil.which(os.path.expanduser(configured)) if configured else shutil.which('openscad')
    if not candidate:
        detail = f'OPENSCAD_BIN={configured!r} is not an executable.' if configured else 'OpenSCAD was not found on PATH.'
        raise ModelError('OPENSCAD_UNAVAILABLE', detail +
                         ' Set OPENSCAD_BIN to the existing native Linux executable (for example /usr/bin/openscad).'
                         ' If it is not installed, see docs/OPENSCAD.md.', 503)
    return Path(candidate).resolve()


def validate_source(source):
    if not isinstance(source, str) or not source.strip():
        raise ModelError('INVALID_SOURCE', 'Provide nonempty .scad source text.')
    try:
        encoded = source.encode('utf-8')
    except UnicodeError as exc:
        raise ModelError('INVALID_SOURCE', 'Source must be valid UTF-8.') from exc
    if len(encoded) > MAX_SOURCE_BYTES or '\0' in source:
        raise ModelError('INVALID_SOURCE', 'Source must be at most 128 KiB and contain no NUL bytes.')
    index = 0
    while index < len(source):
        if source.startswith('//', index):
            end = source.find('\n', index + 2)
            index = len(source) if end == -1 else end + 1
            continue
        if source.startswith('/*', index):
            end = source.find('*/', index + 2)
            if end == -1:
                raise ModelError('INVALID_SOURCE', 'Unterminated source comment.')
            index = end + 2
            continue
        if source[index] == '"':
            index += 1
            while index < len(source) and source[index] != '"':
                index += 2 if source[index] == '\\' else 1
            if index >= len(source):
                raise ModelError('INVALID_SOURCE', 'Unterminated source string.')
            index += 1
            continue
        identifier = IDENTIFIER.match(source, index)
        if identifier:
            token = identifier.group()
            if token in FILE_OPERATIONS:
                raise ModelError('EXTERNAL_ASSET_DENIED',
                                 f'{token} is disabled: no external model assets are allowlisted in this version.')
            index = identifier.end()
            continue
        if ord(source[index]) > 127:
            raise ModelError('INVALID_SOURCE', 'Use ASCII syntax outside comments and strings.')
        index += 1
    return encoded


def stl_dimensions(path):
    """Validate finite triangle coordinates and measure the original STL axes."""
    size = path.stat().st_size
    if not 0 < size <= MAX_STL_BYTES:
        raise ModelError('INVALID_OUTPUT', 'OpenSCAD produced empty or oversized STL output.')
    data = path.read_bytes()
    count = struct.unpack_from('<I', data, 80)[0] if len(data) >= 84 else 0
    if count and len(data) == 84 + 50 * count:
        if count > MAX_TRIANGLES:
            raise ModelError('INVALID_OUTPUT', 'STL exceeds the 200,000 triangle preview limit.')
        vertices = (struct.unpack_from('<3f', data, 84 + 50*i + 12 + 12*j)
                    for i in range(count) for j in range(3))
    else:
        try:
            text = data.decode('ascii')
            if not text.lstrip().startswith('solid') or 'endsolid' not in text:
                raise ValueError('Not an ASCII STL')
            points = re.finditer(r'^[ \t]*vertex[ \t]+(\S+)[ \t]+(\S+)[ \t]+(\S+)[ \t\r]*$', text, re.M)
            vertices = (tuple(float(v) for v in point.groups()) for point in points)
        except (ValueError, UnicodeError) as exc:
            raise ModelError('INVALID_OUTPUT', 'OpenSCAD did not produce a usable STL mesh.') from exc
    low, high = [math.inf]*3, [-math.inf]*3
    vertex_count = 0
    try:
        for vertex in vertices:
            vertex_count += 1
            if vertex_count > MAX_TRIANGLES * 3:
                raise ValueError('Triangle limit exceeded')
            for axis, value in enumerate(vertex):
                if not math.isfinite(value) or abs(value) > 1e9:
                    raise ValueError('Invalid coordinate')
                low[axis], high[axis] = min(low[axis], value), max(high[axis], value)
    except ValueError as exc:
        raise ModelError('INVALID_OUTPUT', 'STL contains invalid coordinates.') from exc
    count = vertex_count // 3
    if not count or vertex_count % 3:
        raise ModelError('INVALID_OUTPUT', 'STL contains an invalid number of triangle vertices.')
    dimensions = {axis: high[i]-low[i] for i, axis in enumerate('xyz')}
    if not all(math.isfinite(v) for v in dimensions.values()) or max(dimensions.values()) <= 0:
        raise ModelError('INVALID_OUTPUT', 'STL has no measurable geometry.')
    return dimensions, count


@dataclass
class Compilation:
    stdout: str
    stderr: str
    dimensions_mm: dict
    triangles: int


class OpenSCADAdapter:
    def __init__(self, executable=None, timeout=30):
        self.executable = locate_openscad(executable)
        self.timeout = timeout
        if not sys.platform.startswith('linux'):
            raise ModelError('SANDBOX_UNAVAILABLE', 'Model compilation currently supports Linux with Bubblewrap only.', 503)
        self.bwrap, self.prlimit = shutil.which('bwrap'), shutil.which('prlimit')
        if not self.bwrap or not self.prlimit:
            raise ModelError('SANDBOX_UNAVAILABLE', 'Install/configure Bubblewrap (bwrap) and util-linux (prlimit).'
                             ' Compilation will not fall back to an unsandboxed process.', 503)

    def _command(self, directory):
        # Only OS runtime files, this source and this output directory are mounted.
        command = [self.prlimit, '--as=2147483648', f'--cpu={math.ceil(self.timeout)+1}',
                   f'--fsize={MAX_STL_BYTES}', '--nofile=128', '--', self.bwrap,
                   '--unshare-all', '--unshare-user', '--die-with-parent', '--new-session', '--cap-drop', 'ALL']
        for path in ('/usr', '/lib', '/lib64', '/bin', '/etc/fonts', '/etc/ld.so.cache'):
            if Path(path).exists():
                command += ['--ro-bind', path, path]
        command += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                    '--dir', '/compiler', '--ro-bind', str(self.executable), '/compiler/openscad',
                    '--dir', '/work', '--ro-bind', str(directory / 'model.scad'), '/work/model.scad',
                    '--bind', str(directory / 'output'), '/work/output', '--chdir', '/work',
                    '--clearenv', '--setenv', 'HOME', '/tmp', '--setenv', 'TMPDIR', '/tmp',
                    '--setenv', 'QT_QPA_PLATFORM', 'offscreen', '--setenv', 'LANG', 'C.UTF-8',
                    '--setenv', 'OPENSCADPATH', '/work/allowed-assets',
                    '--', '/compiler/openscad', '-o', 'output/model.stl', 'model.scad']
        return command

    def compile(self, directory):
        directory = Path(directory).resolve()
        # Revalidate at dispatch, even when called without the job manager.
        validate_source((directory / 'model.scad').read_text())
        output = directory / 'output/model.stl'
        if output.exists() or output.is_symlink():
            raise ModelError('INVALID_OUTPUT', 'Compilation requires a fresh output path.')
        stdout, stderr = self._run(self._command(directory), directory)
        if re.search(r'^[ \t]*ERROR:', stdout + '\n' + stderr, re.M):
            raise ModelError('COMPILE_FAILED', 'OpenSCAD reported an error.', stdout=stdout, stderr=stderr)
        if output.is_symlink() or not output.is_file():
            raise ModelError('INVALID_OUTPUT', 'OpenSCAD did not create an STL file.', stdout=stdout, stderr=stderr)
        try:
            dimensions, triangles = stl_dimensions(output)
        except ModelError as exc:
            exc.stdout, exc.stderr = stdout, stderr
            raise
        return Compilation(stdout, stderr, dimensions, triangles)

    def _run(self, command, directory):
        captured = {name: bytearray() for name in ('stdout', 'stderr')}
        deadline = time.monotonic() + self.timeout
        # Environment secrets and SSH state are never inherited by the compiler.
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        try:
            process = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, shell=False)
        except OSError as exc:
            raise ModelError('COMPILER_START_FAILED', 'Unable to start the configured compiler isolation tools.', 503) from exc
        failure = None
        try:
            with selectors.DefaultSelector() as selector:
                for name in captured:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        failure = ('COMPILE_TIMEOUT', f'Compilation exceeded {self.timeout:g} seconds.')
                        break
                    for key, _ in selector.select(min(remaining, .1)):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        room = MAX_LOG_BYTES - sum(map(len, captured.values()))
                        captured[key.data].extend(chunk[:room])
                        if len(chunk) > room:
                            failure = ('LOG_LIMIT', 'Compilation exceeded the 64 KiB diagnostic limit.')
                            break
                    if failure:
                        break
            if failure:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        stdout, stderr = (captured[name].decode('utf-8', errors='replace') for name in ('stdout', 'stderr'))
        if failure:
            raise ModelError(*failure, stdout=stdout, stderr=stderr)
        if process.returncode:
            code = 'SANDBOX_FAILED' if 'bwrap:' in stderr else 'COMPILE_FAILED'
            raise ModelError(code, f'Compilation failed (exit {process.returncode}); inspect diagnostics.',
                             stdout=stdout, stderr=stderr)
        return stdout, stderr


class ModelJobs:
    """One trusted local backend; immutable source/STL per server-owned revision.

    Two workers and eight pending/running submissions by default. Metadata is
    persisted atomically; interrupted work is failed on restart, never resumed.
    """
    def __init__(self, root, adapter=None, workers=2, capacity=8, max_jobs=100):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store_lock = (self.root / '.lock').open('a')
        try:
            fcntl.flock(self.store_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.store_lock.close()
            raise ModelError('STORE_BUSY', 'Another backend is using this model job directory.', 503) from exc
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(capacity)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='openscad')
        self.jobs, self.latest = {}, {}
        self.max_jobs, self.closed = max_jobs, False
        self.configuration_error = None
        try:
            self.adapter = adapter if adapter is not None else OpenSCADAdapter()
        except ModelError as exc:
            self.adapter, self.configuration_error = None, exc
        self._restore()

    def configuration(self):
        error = self.configuration_error
        return dict(available=error is None, executable=str(self.adapter.executable) if not error else None,
                    error={'code': error.code, 'message': str(error)} if error else None,
                    source_limit_bytes=MAX_SOURCE_BYTES, external_assets=[], sandbox='linux-bubblewrap',
                    printing='separate-job-approval-required')

    def _save(self, job):
        directory = self.root / job['job_id']
        temporary = directory / 'metadata.tmp'
        temporary.write_text(json.dumps(job, allow_nan=False))
        temporary.replace(directory / 'metadata.json')

    def _restore(self):
        for directory in sorted(self.root.iterdir()):
            if not JOB_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            path = directory / 'metadata.json'
            try:
                if path.is_symlink() or path.stat().st_size > 8*MAX_LOG_BYTES:
                    continue
                job = json.loads(path.read_text())
                if job['job_id'] != directory.name or not JOB_ID.fullmatch(job['model_id']):
                    continue
                if job['status'] in ('queued', 'compiling'):
                    job.update(status='error', error={'code': 'INTERRUPTED', 'message': 'Server stopped before compilation completed. Submit a new revision.'})
                    self._save(job)
                self.jobs[job['job_id']] = job
                previous = self.latest.get(job['model_id'])
                if previous is None or self.jobs[previous]['revision'] < job['revision']:
                    self.latest[job['model_id']] = job['job_id']
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _lookup(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or job_id not in self.jobs:
            raise ModelError('JOB_NOT_FOUND', 'Unknown model job ID.', 404)
        return self.jobs[job_id]

    def _public(self, job):
        reply = copy.deepcopy(job)
        reply['is_latest'] = self.latest[job['model_id']] == job['job_id']
        reply['approval_current'] = bool(job['approval']) and reply['is_latest']
        base = '/api/models/' + job['job_id'] + '/artifacts/'
        reply['artifacts'] = {'source': base + 'model.scad',
                              'stl': base + 'model.stl' if job['status'] == 'ready' else None}
        return reply

    def get(self, job_id):
        with self.lock:
            return self._public(self._lookup(job_id))

    def list(self):
        with self.lock:
            return [self._public(job) for job in sorted(self.jobs.values(), key=lambda j: j['created_at'], reverse=True)]

    def submit(self, source, filename='model.scad', parent_job_id=None, *, provenance=None, require_current_parent=False):
        encoded = validate_source(source)
        if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_ .-]{0,80}\.scad', filename) or '..' in filename:
            raise ModelError('INVALID_FILENAME', 'Use a plain filename ending in .scad, without paths.')
        with self.lock:
            if self.closed:
                raise ModelError('SERVICE_CLOSED', 'The model compiler is shutting down.', 503)
            if self.configuration_error:
                raise self.configuration_error
            parent = self._lookup(parent_job_id) if parent_job_id is not None else None
            if parent and require_current_parent and self.latest[parent['model_id']] != parent_job_id:
                raise ModelError('CONTEXT_CHANGED', 'The model changed while AI generation was running; review the latest revision before trying again.', 409)
            if len(self.jobs) >= self.max_jobs or not self.slots.acquire(blocking=False):
                raise ModelError('QUEUE_FULL', 'Model job capacity reached; wait or archive local jobs with the server stopped.', 429)
            job_id = uuid.uuid4().hex
            model_id = parent['model_id'] if parent else job_id
            revision = self.jobs[self.latest[model_id]]['revision'] + 1 if parent else 1
            directory = self.root / job_id
            try:
                directory.mkdir(mode=0o700)
                (directory / 'output').mkdir(mode=0o700)
                (directory / 'model.scad').write_bytes(encoded)
                job = dict(job_id=job_id, model_id=model_id, revision=revision,
                           parent_job_id=parent_job_id, filename=filename, created_at=time.time(),
                           status='queued', source_sha256=hashlib.sha256(encoded).hexdigest(),
                           stl_sha256=None, dimensions_mm=None, triangles=None, stdout='', stderr='',
                           error=None, approval=None, provenance=copy.deepcopy(provenance))
                self._save(job)
                self.jobs[job_id], self.latest[model_id] = job, job_id
                self.pool.submit(self._compile, job_id)
            except BaseException:
                self.slots.release()
                raise
            return self._public(job)

    def _compile(self, job_id):
        try:
            with self.lock:
                job = self.jobs[job_id]
                job['status'] = 'compiling'
                self._save(job)
            result = self.adapter.compile(self.root / job_id)
            path = self.root / job_id / 'output/model.stl'
            with self.lock:
                job.update(status='ready', stdout=result.stdout, stderr=result.stderr,
                           dimensions_mm=result.dimensions_mm, triangles=result.triangles,
                           stl_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                self._save(job)
        except Exception as exc:
            with self.lock:
                error = exc if isinstance(exc, ModelError) else ModelError('INTERNAL_ERROR', 'Compilation failed; inspect server logs.')
                job.update(status='error', error={'code': error.code, 'message': str(error)},
                           stdout=error.stdout, stderr=error.stderr)
                self._save(job)
        finally:
            self.slots.release()

    def artifact(self, job_id, name):
        with self.lock:
            job = self._lookup(job_id)
            if name not in ('model.scad', 'model.stl'):
                raise ModelError('ARTIFACT_NOT_FOUND', 'Unknown model artifact.', 404)
            if name == 'model.stl' and job['status'] != 'ready':
                raise ModelError('ARTIFACT_NOT_READY', 'STL is available only after successful compilation.', 409)
            directory = self.root / job_id
            path = directory / name if name == 'model.scad' else directory / 'output' / name
            if directory.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise ModelError('INVALID_ARTIFACT', 'Artifact path failed validation.', 409)
            try:
                if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_STL_BYTES:
                    raise OSError('Invalid file')
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ModelError('ARTIFACT_NOT_FOUND', 'Artifact is missing or invalid.', 404) from exc
            expected = job['source_sha256'] if name == 'model.scad' else job['stl_sha256']
            if digest != expected:
                raise ModelError('ARTIFACT_CHANGED', 'Artifact changed since compilation; submit a new revision.', 409)
            return path

    def approve(self, job_id, source_sha256, stl_sha256, confirmed):
        with self.lock:
            job = self._lookup(job_id)
            if confirmed is not True:
                raise ModelError('CONFIRMATION_REQUIRED', 'Explicit model approval is required.', 409)
            if job['status'] != 'ready' or self.latest[job['model_id']] != job_id:
                raise ModelError('REVISION_NOT_CURRENT', 'Only the latest successfully compiled revision can be approved.', 409)
            if source_sha256 != job['source_sha256'] or stl_sha256 != job['stl_sha256']:
                raise ModelError('VERSION_MISMATCH', 'Approval must match this exact source and STL.', 409)
            self.artifact(job_id, 'model.scad')
            self.artifact(job_id, 'model.stl')
            job['approval'] = dict(job_id=job_id, revision=job['revision'],
                                   source_sha256=source_sha256, stl_sha256=stl_sha256,
                                   approved_at=time.time(), scope='model-only')
            self._save(job)
            return self._public(job)

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True)
        with self.lock:
            if not self.store_lock.closed:
                self.store_lock.close()
