"""Model dashboard with optional, explicitly configured approved print jobs."""
import argparse
import atexit
import errno
import getpass
import os
from pathlib import Path
import sys
import socket
import warnings

from flask import Flask, jsonify, render_template, request, send_file

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from printing_agent.agent import PrintingAgent  # noqa: E402
from printing_agent.openscad import ModelError, ModelJobs, MAX_SOURCE_BYTES  # noqa: E402
from printing_agent.generation import GenerationJobs  # noqa: E402
from printing_agent.providers import DEFAULT_GROQ_MODEL, GroqProvider  # noqa: E402
from printing_agent.print_jobs import PrintJobs  # noqa: E402
from printing_agent.print_transport import SSHPrintTransport  # noqa: E402
from remote_access import install_remote_access, is_loopback, remote_password  # noqa: E402


def create_app(agent, remote_login_password=None, models=None, generations=None, prints=None):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = MAX_SOURCE_BYTES * 8
    app.extensions['model_jobs'] = models
    app.extensions['generation_jobs'] = generations
    app.extensions['print_jobs'] = prints
    install_remote_access(app, remote_login_password)

    @app.before_request
    def model_controls():
        if request.method == 'POST' and request.path.startswith(('/api/models', '/api/prints')):
            if request.headers.get('X-Lab-Control') != 'local':
                return jsonify(error={'code': 'CONTROL_HEADER_REQUIRED', 'message': 'Use the local dashboard controls.'}), 403
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(error={'code': 'ORIGIN_REJECTED', 'message': 'Cross-origin model controls are disabled.'}), 403

    def model_service():
        if models is None:
            raise ModelError('COMPILER_UNAVAILABLE', 'Start the dashboard process to enable model compilation.', 503)
        return models

    def generation_service():
        if generations is None:
            raise ModelError('GENERATOR_UNAVAILABLE', 'Start the configured backend to enable AI model generation.', 503)
        return generations

    def print_service():
        if prints is None:
            raise ModelError('PRINT_CONNECTION_DISABLED', 'Start a backend with print-job support configured.', 503)
        return prints

    def body(allowed, required):
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - set(allowed) or not set(required) <= set(data):
            raise ModelError('INVALID_REQUEST', 'Invalid or missing model request fields.')
        return data

    @app.errorhandler(ModelError)
    def model_error(exc):
        return jsonify(ok=False, error={'code': exc.code, 'message': str(exc)}), exc.http_status

    @app.after_request
    def model_cache(response):
        if request.path.startswith(('/api/models', '/api/prints')):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/config')
    def config():
        return jsonify(printer_mode='mock', simulated=True, printer_read_only=True,
                       model_compilation='available' if models and models.configuration()['available'] else 'unavailable',
                       model_generation='available' if generations and generations.configuration()['available'] else 'unavailable',
                       printing=prints.configuration() if prints else 'disabled')

    @app.get('/api/prints/config')
    def print_config():
        return jsonify(print_service().configuration())

    @app.get('/api/prints/printer')
    def print_printer():
        return jsonify(print_service().printer_status())

    @app.get('/api/prints')
    def print_list():
        return jsonify(jobs=print_service().list())

    @app.post('/api/prints')
    def print_prepare():
        data = body(('model_job_id', 'profile_id'), ('model_job_id', 'profile_id'))
        return jsonify(print_service().prepare(**data)), 202

    @app.get('/api/prints/<job_id>')
    def print_status(job_id):
        return jsonify(print_service().get(job_id))

    @app.get('/api/prints/<job_id>/status')
    def print_refresh(job_id):
        return jsonify(print_service().refresh(job_id))

    @app.get('/api/prints/<job_id>/gcode')
    def print_artifact(job_id):
        return send_file(print_service().artifact(job_id), mimetype='text/plain',
                         as_attachment=True, download_name='photonics_' + job_id + '.gcode',
                         conditional=False, max_age=0)

    @app.post('/api/prints/<job_id>/approve')
    def print_approve(job_id):
        data = body(('gcode_sha256', 'confirmed', 'setup_confirmed'),
                    ('gcode_sha256', 'confirmed', 'setup_confirmed'))
        return jsonify(print_service().approve(job_id, **data))

    @app.post('/api/prints/<job_id>/start')
    def print_start(job_id):
        data = body(('token',), ('token',))
        return jsonify(print_service().start(job_id, **data)), 202

    @app.get('/api/models/config')
    def model_config():
        config = model_service().configuration()
        config['backend_pid'] = os.getpid()
        config['generation'] = generations.configuration() if generations else {
            'available':False, 'error':{'code':'GENERATOR_UNAVAILABLE', 'message':'AI provider is not configured.'}}
        return jsonify(config)

    @app.get('/api/models/generations')
    def generation_list():
        return jsonify(requests=generation_service().list())

    @app.post('/api/models/generate')
    def generation_submit():
        data = body(('description', 'parent_job_id', 'previous_request_id', 'base_source'), ('description',))
        return jsonify(generation_service().submit(**data)), 202

    @app.get('/api/models/generations/<request_id>')
    def generation_status(request_id):
        return jsonify(generation_service().get(request_id))

    @app.get('/api/models/example')
    def model_example():
        return jsonify(filename='parametric_spacer.scad', origin='hand-written example, not AI-generated',
                       source=(ROOT.parent / 'examples/parametric_spacer.scad').read_text())

    @app.get('/api/models')
    def model_list():
        return jsonify(jobs=model_service().list())

    @app.post('/api/models')
    def model_submit():
        data = body(('source', 'filename', 'parent_job_id'), ('source',))
        job = model_service().submit(data['source'], data.get('filename', 'model.scad'), data.get('parent_job_id'))
        return jsonify(job), 202

    @app.get('/api/models/<job_id>')
    def model_status(job_id):
        return jsonify(model_service().get(job_id))

    @app.get('/api/models/<job_id>/artifacts/<name>')
    def model_artifact(job_id, name):
        service = model_service()
        path, job = service.artifact(job_id, name), service.get(job_id)
        suffix = 'scad' if name == 'model.scad' else 'stl'
        return send_file(path, mimetype='text/plain' if suffix == 'scad' else 'application/octet-stream',
                         as_attachment=True, download_name=f'model-v{job["revision"]}.{suffix}',
                         conditional=False, max_age=0)

    @app.post('/api/models/<job_id>/approve')
    def model_approve(job_id):
        data = body(('source_sha256', 'stl_sha256', 'confirmed'), ('source_sha256', 'stl_sha256', 'confirmed'))
        return jsonify(model_service().approve(job_id, **data))

    @app.get('/api/printer/<command>')
    def printer_read(command):
        if command not in ('describe', 'status', 'position', 'capabilities'):
            return jsonify(error='Unsupported read-only command'), 404
        reply = agent.handle(dict(version=1, specialist='3d-printing',
                                  task_id='dashboard-status', request={'command': command}))
        return jsonify(reply), 200 if reply['ok'] else 503

    return app


def prepare_startup(args, parser):
    if not 1 <= args.port <= 65535:
        parser.error('Use a port between 1 and 65535.')
    # Fail before asking for a key if an older server already owns the address.
    try:
        addresses = socket.getaddrinfo(args.host, args.port, type=socket.SOCK_STREAM)
        family, kind, protocol, _, address = addresses[0]
        with socket.socket(family, kind, protocol) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(address)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            parser.error(f'Port {args.port} is already in use. Stop the previous dashboard in its terminal with Ctrl+C before starting this one.')
        parser.error('Cannot bind the requested dashboard address. Check --host and --port.')
    if args.prompt_groq_key:
        if os.environ.get('MODEL_PROVIDER', 'groq') != 'groq':
            parser.error('--prompt-groq-key requires MODEL_PROVIDER=groq.')
        if not sys.stdin.isatty():
            parser.error('Run --prompt-groq-key in an interactive terminal so key entry can stay hidden.')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                key = getpass.getpass('Paste your Groq API key, then press Enter (hidden): ').strip()
            GroqProvider(api_key=key, model=DEFAULT_GROQ_MODEL)  # Key syntax only; no HTTP call.
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            parser.error('Hidden key entry was cancelled or unavailable. No dashboard was started.')
        except ModelError:
            parser.error('No valid nonempty key was entered. Run again and paste the actual Groq API key at the hidden prompt.')
        os.environ['GROQ_API_KEY'] = key
        print('Groq key received for this backend process. No key was saved or API request sent.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--printer-mode', choices=['mock'], default='mock')
    parser.add_argument('--print-backend', choices=['disabled', 'ssh'], default='disabled',
                        help='Explicitly enable the separate print-job adapter; default is disabled.')
    parser.add_argument('--printer-host', help='Hostname/IP of the Ender-3 V3 KE for approved print jobs.')
    parser.add_argument('--ssh-control-path', default='/tmp/photonics-printer-control')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--prompt-groq-key', action='store_true', help='Ask for the Groq key privately in this terminal before starting; nothing is saved.')
    args = parser.parse_args(argv)
    if args.print_backend == 'ssh' and not args.printer_host:
        parser.error('--print-backend ssh requires --printer-host. Printer environment variables do not enable printing.')
    if args.printer_host and args.print_backend != 'ssh':
        parser.error('--printer-host requires --print-backend ssh.')
    prepare_startup(args, parser)
    login_password = None
    if not is_loopback(args.host):
        try:
            login_password, password_file = remote_password(ROOT)
        except ValueError as exc:
            parser.error(str(exc))
        print('Remote browser login: username lab', flush=True)
        print(f'Password file: {password_file}' if password_file else
              'Password: LAB_REMOTE_PASSWORD from your environment', flush=True)
    agent = PrintingAgent(mode='mock')
    atexit.register(agent.close)
    try:
        models = ModelJobs(Path(os.environ.get('OPENSCAD_JOBS_DIR', str(ROOT / 'results/models'))))
    except ModelError as exc:
        parser.error(str(exc) + ' Stop the previous dashboard before restarting.')
    atexit.register(models.close)
    generations = GenerationJobs(models)
    atexit.register(generations.close)
    transport = SSHPrintTransport(args.printer_host, args.ssh_control_path) if args.print_backend == 'ssh' else None
    try:
        prints = PrintJobs(models.root / 'prints', models, transport=transport)
    except ModelError as exc:
        parser.error(str(exc))
    atexit.register(prints.close)
    configured = generations.configuration()
    print(f'Backend PID: {os.getpid()} · AI configuration: {"ready" if configured["available"] else "unavailable"}', flush=True)
    if configured['error']:
        print('AI configuration: ' + configured['error']['message'], flush=True)
    print(f'Open http://127.0.0.1:{args.port} · model generation/compilation · mock printer reads', flush=True)
    print(f'Print-job backend: {args.print_backend}. Nothing is submitted on startup or model approval.', flush=True)
    create_app(agent, login_password, models, generations, prints).run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == '__main__':
    main()
