"""Separate print-job helper sent over shared SSH. Python 3.8 compatible.

No import-time I/O. Only status and an exact hashed-file submission are exposed.
The positioning watchdog and its request validator are unchanged.
"""
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time

from printer_watchdog import API, LOCK_PATH

MAX_BYTES = 16 * 1024 * 1024
HEX = re.compile(r'[a-f0-9]{64}\Z')
ID = re.compile(r'[a-f0-9]{32}\Z')


def snapshot(api):
    result = api.call('objects/query', {'objects': {
        'webhooks': ['state'], 'print_stats': None, 'virtual_sdcard': None,
        'pause_resume': ['is_paused'], 'toolhead': ['position', 'homed_axes', 'axis_minimum', 'axis_maximum'],
        'motion_report': ['live_velocity'], 'heater_bed': ['temperature', 'target'],
        'extruder': ['temperature', 'target'], 'gcode_move': ['speed_factor', 'extrude_factor', 'homing_origin'],
        'configfile': ['settings'], 'bed_mesh': ['profiles']}})
    state = result['status']
    settings = state.pop('configfile')['settings']
    sdroot = os.path.expanduser(settings['virtual_sdcard']['path'])
    if not os.path.isabs(sdroot) or os.path.realpath(sdroot) != os.path.normpath(sdroot):
        raise ValueError('Virtual SD directory must be an absolute path without symlinks.')
    identity = api.call('info').get('hostname')
    if not isinstance(identity, str) or not identity:
        raise ValueError('Printer identity is missing.')
    config_hash = hashlib.sha256(json.dumps({'identity': identity, 'settings': settings},
                                           sort_keys=True, allow_nan=False).encode()).hexdigest()
    state['printer_identity'] = identity
    state['configuration_sha256'] = config_hash
    state['nozzle_mm'] = float(settings['extruder']['nozzle_diameter'])
    state['filament_mm'] = float(settings['extruder']['filament_diameter'])
    state['has_default_mesh'] = 'default' in state.pop('bed_mesh')['profiles']
    return state, sdroot


def preflight(state):
    if (state['webhooks']['state'] != 'ready'
            or state['print_stats']['state'] not in ('standby', 'complete', 'cancelled')
            or state['virtual_sdcard']['is_active'] is not False
            or state['pause_resume']['is_paused'] is not False):
        raise ValueError('Printer must be ready, idle and not paused.')
    velocity = state['motion_report']['live_velocity']
    if not isinstance(velocity, (int, float)) or not math.isfinite(velocity) or abs(velocity) > .001:
        raise ValueError('Printer must be stationary.')
    for name in ('heater_bed', 'extruder'):
        target = state[name]['target']
        if not isinstance(target, (int, float)) or not math.isfinite(target) or target != 0:
            raise ValueError('Turn off existing heater targets before preparing a new print.')
    if abs(state['nozzle_mm'] - .4) > 1e-9 or abs(state['filament_mm'] - 1.75) > 1e-9:
        raise ValueError('This adapter requires a configured 0.4 mm nozzle and 1.75 mm filament.')
    if not state['has_default_mesh']:
        raise ValueError('A saved default bed mesh is required by this printing profile.')
    for index, maximum in enumerate((220, 220, 245)):
        lower, upper = state['toolhead']['axis_minimum'][index], state['toolhead']['axis_maximum'][index]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lower, upper)) or lower > 0 or upper < maximum:
            raise ValueError('Printer bounds do not contain the configured print region.')
    move = state['gcode_move']
    if move['speed_factor'] != 1 or move['extrude_factor'] != 1 or any(move['homing_origin']):
        raise ValueError('Restore normal speed/extrusion overrides and zero G-code offsets before printing.')


def handle(request, api_factory=API):
    if not isinstance(request, dict) or request.get('action') not in ('status', 'submit'):
        raise ValueError('Unsupported print-job request.')
    if request['action'] == 'status' and set(request) != {'action'}:
        raise ValueError('Unexpected status fields.')
    if request['action'] == 'submit':
        if set(request) != {'action', 'job_id', 'sha256', 'gcode', 'configuration_sha256'}:
            raise ValueError('Invalid print submission fields.')
        if (not isinstance(request['job_id'], str) or not ID.fullmatch(request['job_id'])
                or not isinstance(request['sha256'], str) or not HEX.fullmatch(request['sha256'])
                or not isinstance(request['configuration_sha256'], str)
                or not HEX.fullmatch(request['configuration_sha256'])):
            raise ValueError('Invalid print identity/hash.')
    api = api_factory()
    lock = None
    submitted = False
    try:
        if request['action'] == 'submit':
            # Also exclude the retained positioning runner during submission.
            lock = open(LOCK_PATH, 'a')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state, sdroot = snapshot(api)
        if request['action'] == 'status':
            try:
                preflight(state)
                state['ready_for_print'], state['readiness_error'] = True, None
            except (ValueError, KeyError, TypeError) as exc:
                state['ready_for_print'], state['readiness_error'] = False, str(exc)
            return {'ok': True, 'simulated': False, 'state': state}
        preflight(state)
        if state['configuration_sha256'] != request['configuration_sha256']:
            raise ValueError('Printer configuration changed; prepare and approve a new job.')
        data = base64.b64decode(request['gcode'], validate=True)
        if not 0 < len(data) <= MAX_BYTES or hashlib.sha256(data).hexdigest() != request['sha256']:
            raise ValueError('Uploaded file does not match the approved G-code hash.')
        # The directory comes from this printer's virtual_sdcard configuration.
        # Per-job immutable filenames and durable launch markers prevent replay.
        name = 'photonics_' + request['job_id'] + '.gcode'
        path = os.path.join(sdroot, name)
        marker = path + '.launch.json'
        if os.path.lexists(path) or os.path.lexists(marker):
            raise ValueError('This print job was already submitted; do not retry it.')
        if not stat.S_ISDIR(os.stat(sdroot).st_mode):
            raise ValueError('Virtual SD directory is unavailable.')
        if os.statvfs(sdroot).f_bavail * os.statvfs(sdroot).f_frsize < len(data) + 8 * 1024 * 1024:
            raise ValueError('Insufficient printer storage.')
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with open(path, 'rb') as stream:
            if hashlib.sha256(stream.read(MAX_BYTES + 1)).hexdigest() != request['sha256']:
                raise ValueError('Printer file hash failed validation.')
        # Query again after upload, immediately before recording/dispatching launch.
        current, current_root = snapshot(api)
        preflight(current)
        if current_root != sdroot or current['configuration_sha256'] != request['configuration_sha256']:
            raise ValueError('Printer configuration changed during upload.')
        with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as stream:
            json.dump({'job_id': request['job_id'], 'sha256': request['sha256'],
                       'status': 'dispatching', 'created_at': time.time()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        submitted = True
        # A fixed native command selects only our immutable, hash-checked filename.
        # No user/model text is ever concatenated into G-code.
        api.call('gcode/script', {'script': 'SDCARD_PRINT_FILE FILENAME=' + name}, timeout=8)
        return {'ok': True, 'simulated': False, 'filename': name, 'submission': 'accepted'}
    except Exception as exc:
        return {'ok': False, 'simulated': False, 'command_may_have_executed': submitted,
                'error': str(exc)}
    finally:
        api.close()
        if lock is not None:
            lock.close()


def main():
    try:
        raw = sys.stdin.buffer.read(24 * 1024 * 1024 + 1)
        if len(raw) > 24 * 1024 * 1024:
            raise ValueError('Oversized print request.')
        reply = handle(json.loads(raw))
    except Exception:
        reply = {'ok': False, 'error': 'Invalid print helper request.', 'command_may_have_executed': False}
    print(json.dumps(reply, allow_nan=False))


if __name__ == '__main__':
    main()
