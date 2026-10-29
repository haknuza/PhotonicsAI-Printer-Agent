"""Print profile descriptions and bounded G-code inspection; no slicing/hardware I/O."""
import math
import re

from .openscad import ModelError

MAX_GCODE_BYTES = 16 * 1024 * 1024
# Return to the already-homed XY origin after lifting clear of the part. Never
# re-home Z over a finished print or delegate completion motion to a macro.
PRINT_END_GCODE = 'M104 S0\nM140 S0\nM107\nG91\nG1 Z5 F300\nG90\nG1 X0 Y0 F1800\nM84'
PRINT_END_DESCRIPTION = 'On completion: heaters off, lift Z 5 mm, return to X=0 / Y=0 at 30 mm/s; Z stays raised.'
PROFILES = {
    'ender3v3ke-pla-04': {'name': 'Ender-3 V3 KE · PLA · 0.4 mm', 'material': 'PLA',
                        'nozzle_mm': 0.4, 'nozzle_c': 210, 'bed_c': 60},
    'ender3v3ke-petg-04': {'name': 'Ender-3 V3 KE · PETG · 0.4 mm', 'material': 'PETG',
                         'nozzle_mm': 0.4, 'nozzle_c': 240, 'bed_c': 80},
}


def profile_info(profile_id):
    if not isinstance(profile_id, str) or profile_id not in PROFILES:
        raise ModelError('PROFILE_REQUIRED', 'Select the material and 0.4 mm nozzle profile.')
    return dict(PROFILES[profile_id], id=profile_id, layer_height_mm=0.2,
                infill_percent=15, supports=False, print_speed_mm_s=40,
                bed_size_mm=[220, 220], maximum_model_height_mm=240,
                end_behavior=PRINT_END_DESCRIPTION,
                qualification='Creality vendor presets with displayed bounded overrides; not physically print-tested.')


def inspect_gcode(data, profile):
    """Reject commands outside this fixed slicer's dialect; bound travel and heat.

    This validates explicit toolpath coordinates, not collision-free physical setup
    or the internal motion of the separately reviewed firmware homing procedure.
    """
    if not 0 < len(data) <= MAX_GCODE_BYTES:
        raise ModelError('INVALID_GCODE', 'Sliced output is empty or exceeds 16 MiB.', 409)
    try:
        text = data.decode('ascii')
    except UnicodeError as exc:
        raise ModelError('INVALID_GCODE', 'Slicer output must be ASCII G-code.', 409) from exc
    lines = [(number, raw.split(';', 1)[0].strip())
             for number, raw in enumerate(text.splitlines(), 1) if raw.split(';', 1)[0].strip()]
    finish = PRINT_END_GCODE.splitlines()
    if [line for _, line in lines[-len(finish):]] != finish:
        raise ModelError('INVALID_GCODE', 'Expected the fixed heater-off, Z-lift and XY-home completion sequence.', 409)
    absolute, relative_e, homed = True, False, False
    position, e_position, nozzle, bed, feed = {}, 0.0, 0.0, 0.0, 0.0
    extrusions, segments, commands = 0, [], 0
    low, high = {a: math.inf for a in 'xyz'}, {a: -math.inf for a in 'xyz'}
    allowed = {'G0': 'XYZEF', 'G1': 'XYZEF', 'G21': '', 'G90': '', 'G91': '',
               'G92': 'E', 'M82': '', 'M83': '', 'M104': 'S', 'M109': 'S',
               'M140': 'S', 'M190': 'S', 'M106': 'S', 'M107': '', 'M84': ''}
    for number, line in lines:
        commands += 1
        if line == 'G28':
            if homed:
                raise ModelError('INVALID_GCODE', 'Unexpected repeated homing in sliced output.', 409)
            homed, position = True, {}
            continue
        if line == 'BED_MESH_PROFILE LOAD=default':
            if not homed or extrusions:
                raise ModelError('INVALID_GCODE', 'Unexpected bed mesh command.', 409)
            continue
        parts = line.split(maxsplit=1)
        command, tail = parts[0], parts[1] if len(parts) > 1 else ''
        if command not in allowed:
            raise ModelError('INVALID_GCODE', f'Unsupported sliced command on line {number}: {command}.', 409)
        params = {}
        cursor = 0
        pattern = re.compile(r'\s*([A-Z])([-+]?(?:\d+(?:\.\d*)?|\.\d+))')
        while cursor < len(tail):
            match = pattern.match(tail, cursor)
            if not match or match[1] not in allowed[command] or match[1] in params:
                raise ModelError('INVALID_GCODE', f'Invalid sliced parameters on line {number}.', 409)
            params[match[1]] = float(match[2])
            cursor = match.end()
        if any(not math.isfinite(v) for v in params.values()):
            raise ModelError('INVALID_GCODE', 'Nonfinite sliced coordinate.', 409)
        if command in ('G90', 'G91'):
            absolute = command == 'G90'
        elif command in ('M82', 'M83'):
            relative_e = command == 'M83'
        elif command == 'G92':
            if set(params) != {'E'}:
                raise ModelError('INVALID_GCODE', 'Only the extrusion origin may be reset.', 409)
            e_position = params['E']
        elif command in ('M104', 'M109', 'M140', 'M190'):
            maximum = profile['nozzle_c'] if command in ('M104', 'M109') else profile['bed_c']
            if set(params) != {'S'} or not 0 <= params['S'] <= maximum:
                raise ModelError('INVALID_GCODE', 'Temperature exceeds the selected profile.', 409)
            if command in ('M104', 'M109'):
                nozzle = params['S']
            else:
                bed = params['S']
        elif command == 'M106':
            if set(params) != {'S'} or not 0 <= params['S'] <= 255:
                raise ModelError('INVALID_GCODE', 'Invalid fan speed.', 409)
        elif command in ('G0', 'G1'):
            if not homed:
                raise ModelError('INVALID_GCODE', 'Movement appears before job homing.', 409)
            before = dict(position)
            for axis in 'XYZ':
                if axis in params:
                    key = axis.lower()
                    if not absolute and key not in position:
                        raise ModelError('INVALID_GCODE', 'Relative travel has an unknown start.', 409)
                    position[key] = params[axis] if absolute else position[key] + params[axis]
                    maximum = 245 if key == 'z' else 220
                    if not 0 <= position[key] <= maximum:
                        raise ModelError('OUT_OF_BOUNDS', f'Sliced {axis} travel exceeds the printable region.', 409)
                    low[key], high[key] = min(low[key], position[key]), max(high[key], position[key])
            if 'F' in params:
                feed = params['F']
            if not 0 < feed <= 7200:
                raise ModelError('INVALID_GCODE', 'Sliced speed exceeds 120 mm/s.', 409)
            extrusion = params.get('E', 0) if relative_e else params.get('E', e_position) - e_position
            if 'E' in params:
                e_position = e_position + params['E'] if relative_e else params['E']
            if abs(extrusion) > 50:
                raise ModelError('INVALID_GCODE', 'Unexpected extrusion length.', 409)
            if extrusion > 0:
                if nozzle != profile['nozzle_c'] or bed != profile['bed_c'] or len(position) != 3:
                    raise ModelError('INVALID_GCODE', 'Extrusion lacks configured temperature or position.', 409)
                extrusions += 1
                if all(a in before for a in 'xy') and len(segments) < 50000:
                    segments.append([round(before['x'], 3), round(before['y'], 3),
                                     round(position['x'], 3), round(position['y'], 3)])
    if not extrusions or not segments or nozzle or bed:
        raise ModelError('INVALID_GCODE', 'Expected a nonempty toolpath and heaters off at the end.', 409)
    estimate = re.search(r'; estimated printing time \(normal mode\) = ([^\r\n]+)', text)
    filament = re.search(r'; filament used \[mm\] = ([\d.]+)', text)
    return {'commands': commands, 'extrusion_moves': extrusions,
            'bounds_mm': {a: [low[a], high[a]] for a in 'xyz'},
            'estimated_time': estimate[1] if estimate else 'Unavailable',
            'filament_mm': float(filament[1]) if filament else None,
            'preview_segments': segments, 'preview_truncated': extrusions > len(segments)}
