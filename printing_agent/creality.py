"""Creality Print CLI with installed vendor presets and bounded local overrides.

No GUI state, cloud credentials or printer connections are used for slicing.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil

from .openscad import ModelError, OpenSCADAdapter
from .slicing import MAX_GCODE_BYTES, PRINT_END_GCODE, profile_info

MACHINE = 'Creality Ender-3 V3 KE 0.4 nozzle'
PROCESS = '0.20mm Standard @' + MACHINE
FILAMENT = {'PLA': 'CR-PLA @' + MACHINE, 'PETG': 'CR-PETG @' + MACHINE}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2) + '\n'


class VendorPresets:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ModelError('CREALITY_PRESETS_MISSING', 'Creality Print bundled profiles were not found.', 503)

    def load(self, kind, name, seen=None, provenance=None):
        seen = set() if seen is None else seen
        provenance = [] if provenance is None else provenance
        if kind not in ('machine', 'process', 'filament') or not isinstance(name, str) or '/' in name or '\\' in name or '..' in name:
            raise ModelError('CREALITY_PRESET_INVALID', 'Invalid vendor profile reference.', 503)
        if name in seen or len(seen) >= 16:
            raise ModelError('CREALITY_PRESET_INVALID', 'Vendor profile inheritance is cyclic or too deep.', 503)
        # Only bundled vendor presets are searched. User/cloud configuration is
        # never imported, nor can browser requests supply profile paths.
        preferred = self.root / 'Creality' / kind / (name + '.json')
        candidates = [preferred] if preferred.is_file() else list(self.root.glob('*/' + kind + '/' + name + '.json'))
        if len(candidates) != 1:
            raise ModelError('CREALITY_PRESETS_MISSING', f'Expected one bundled {kind} preset: {name}.', 503)
        path = candidates[0]
        if path.is_symlink() or not path.resolve().is_relative_to(self.root) or path.stat().st_size > 1024 * 1024:
            raise ModelError('CREALITY_PRESET_INVALID', 'Vendor preset path/size failed validation.', 503)
        try:
            data = path.read_bytes()
            value = json.loads(data)
            if not isinstance(value, dict) or value.get('type') != kind or value.get('name') != name:
                raise ValueError('Invalid preset identity')
        except (OSError, ValueError) as exc:
            raise ModelError('CREALITY_PRESET_INVALID', 'Unable to read the selected vendor preset.', 503) from exc
        provenance.append({'path': str(path.relative_to(self.root)), 'sha256': hashlib.sha256(data).hexdigest()})
        parent = value.pop('inherits', '')
        result = self.load(kind, parent, seen | {name}, provenance)[0] if parent else {}
        result.update(value)
        # The CLI uses this marker to match compatible_printers against the
        # resolved preset's name; a user preset would require an inherits link.
        result.update(instantiation='true', **{'from': 'system'})
        return result, provenance


def resolved_profile(profile_id, presets, engine_sha256):
    """Flatten exact bundled profiles, then apply the displayed starting settings."""
    profile = profile_info(profile_id)
    machine, machine_sources = presets.load('machine', MACHINE)
    process, process_sources = presets.load('process', PROCESS)
    filament, filament_sources = presets.load('filament', FILAMENT[profile['material']])
    if machine.get('printer_model') != 'Creality Ender-3 V3 KE' or [str(v) for v in machine.get('nozzle_diameter', [])] != ['0.4']:
        raise ModelError('CREALITY_PRESET_INVALID', 'The bundled preset is not the expected Ender-3 V3 KE / 0.4 mm printer.', 503)
    # Keep the current application's print envelope and startup behavior. The
    # vendor purge line uses negative X and its defaults are much faster; those
    # are not silently enabled by replacing the slicing engine.
    machine.update({
        'gcode_flavor': 'klipper', 'use_relative_e_distances': '1',
        'emit_machine_limits_to_gcode': '0', 'disable_m73': '1',
        'single_extruder_multi_material': '0', 'nozzle_diameter': ['0.4'],
        'printable_area': ['0x0', '220x0', '220x220', '0x220'], 'printable_height': '240',
        'machine_start_gcode': 'G90\nM83\nG28\nBED_MESH_PROFILE LOAD=default\n'
            'M140 S[bed_temperature_initial_layer_single]\nM104 S[nozzle_temperature_initial_layer]\n'
            'M190 S[bed_temperature_initial_layer_single]\nM109 S[nozzle_temperature_initial_layer]',
        'machine_end_gcode': PRINT_END_GCODE,
        'before_layer_change_gcode': '', 'layer_change_gcode': '',
        'change_filament_gcode': '', 'machine_pause_gcode': '',
        'thumbnails': '', 'thumbnails_format': 'PNG', 'z_hop': ['0.2'], 'z_hop_types': ['Normal Lift'],
        'retraction_length': ['0.8'], 'retraction_speed': ['30'], 'deretraction_speed': ['30'],
    })
    process.update({
        'layer_height': '0.2', 'initial_layer_print_height': '0.2', 'wall_loops': '3',
        'top_shell_layers': '4', 'bottom_shell_layers': '4', 'sparse_infill_density': '15%',
        'enable_support': '0', 'enable_prime_tower': '0', 'enable_arc_fitting': '0',
        'exclude_object': '0', 'gcode_label_objects': '0', 'gcode_add_line_number': '0',
        'post_process': [], 'filename_format': 'print.gcode', 'print_sequence': 'by layer',
        'skirt_loops': '2', 'skirt_distance': '3', 'skirt_height': '1', 'skirt_speed': '20',
        'brim_type': 'no_brim', 'brim_width': '0', 'travel_speed': '120',
        'outer_wall_speed': '30', 'inner_wall_speed': '40', 'sparse_infill_speed': '40',
        'internal_solid_infill_speed': '40', 'top_surface_speed': '30', 'gap_infill_speed': '25',
        'initial_layer_speed': '20', 'initial_layer_infill_speed': '20', 'bridge_speed': '25',
        'internal_bridge_speed': '100%', 'overhang_1_4_speed': '0', 'overhang_2_4_speed': '30',
        'overhang_3_4_speed': '20', 'overhang_4_4_speed': '10',
        'default_acceleration': '0', 'outer_wall_acceleration': '0', 'inner_wall_acceleration': '0',
        'sparse_infill_acceleration': '0', 'internal_solid_infill_acceleration': '0',
        'initial_layer_acceleration': '0', 'top_surface_acceleration': '0',
        'travel_acceleration': '0', 'bridge_acceleration': '0', 'accel_to_decel_enable': '0',
        'default_jerk': '0', 'outer_wall_jerk': '0', 'inner_wall_jerk': '0',
        'infill_jerk': '1', 'top_surface_jerk': '1', 'initial_layer_jerk': '1', 'travel_jerk': '1',
    })
    filament.update({
        'filament_type': [profile['material']], 'filament_diameter': ['1.75'],
        'nozzle_temperature': [str(profile['nozzle_c'])],
        'nozzle_temperature_initial_layer': [str(profile['nozzle_c'])],
        'enable_pressure_advance': ['0'], 'filament_start_gcode': [''], 'filament_end_gcode': [''],
        'enable_filament_ramming': '0', 'activate_chamber_temp_control': ['0'],
        'activate_air_filtration': ['0'], 'additional_cooling_fan_speed': ['0'],
        'filament_max_volumetric_speed': ['8'], 'material_flow_dependent_temperature': ['0'],
    })
    for bed in ('hot_plate', 'textured_plate', 'cool_plate', 'eng_plate', 'epoxy_resin_plate'):
        filament[bed + '_temp'] = [str(profile['bed_c'])]
        filament[bed + '_temp_initial_layer'] = [str(profile['bed_c'])]
    bundle = {'machine': machine, 'process': process, 'filament': filament,
              'vendor_sources': machine_sources + process_sources + filament_sources,
              'engine_sha256': engine_sha256, 'slicer_engine': 'creality-print'}
    source = encoded(bundle)
    profile.update(slicer_engine='creality-print', vendor_machine=MACHINE,
                   vendor_process=PROCESS, vendor_filament=FILAMENT[profile['material']],
                   engine_sha256=engine_sha256,
                   settings_sha256=hashlib.sha256(source.encode()).hexdigest(),
                   qualification='Creality vendor presets with displayed bounded overrides; not physically print-tested.')
    return profile, source


class CrealityPrint:
    engine_id = 'creality-print'
    engine_name = 'Creality Print'

    def __init__(self, executable=None, resources=None):
        local = Path(__file__).resolve().parents[1] / '.tools/creality/squashfs-root'
        configured = executable or os.environ.get('CREALITY_PRINT_BIN')
        found = shutil.which(configured) if configured else None
        if not configured:
            for candidate in (str(local / 'bin/CrealityPrint'), str(local / 'usr/bin/CrealityPrint'),
                              str(local / 'usr/bin/creality_print'), 'CrealityPrint', 'creality-print', 'creality_print'):
                found = shutil.which(candidate)
                if found:
                    break
        if not found:
            raise ModelError('CREALITY_UNAVAILABLE', 'Install/extract Creality Print and configure CREALITY_PRINT_BIN. PrusaSlicer is not used as a fallback.', 503)
        self.executable = Path(found).resolve()
        setting = resources or os.environ.get('CREALITY_PRINT_RESOURCES')
        ancestors = list(self.executable.parents)[:4]
        candidates = [Path(setting)] if setting else [parent / suffix for parent in ancestors for suffix in
            ('resources', 'share/CrealityPrint/resources', 'usr/share/CrealityPrint/resources')]
        self.resources = next((path.resolve() for path in candidates if (path / 'profiles/Creality').is_dir()), None)
        if self.resources is None:
            raise ModelError('CREALITY_PRESETS_MISSING', 'Set CREALITY_PRINT_RESOURCES to the installed resources folder containing profiles/Creality.', 503)
        self.presets = VendorPresets(self.resources / 'profiles')
        self.runtime = local if self.executable.is_relative_to(local) else (
            self.executable.parent.parent if self.executable.parent.name == 'bin' else self.executable.parent)
        compatibility = os.environ.get('CREALITY_PRINT_COMPAT_DIR')
        self.compat = Path(compatibility).resolve() if compatibility else (
            local.parent / 'compat' if self.executable.is_relative_to(local) and (local.parent / 'compat/manifest.json').is_file() else None)
        self.loader = None
        self.compat_digest = ''
        if self.compat:
            try:
                manifest = self.compat / 'manifest.json'
                value = json.loads(manifest.read_text())
                self.loader = self.compat / value['loader']
                if not self.loader.is_file() or not self.loader.resolve().is_relative_to(self.compat):
                    raise ValueError('Invalid private loader')
                self.compat_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ModelError('CREALITY_COMPAT_INVALID', 'Private compatibility runtime is incomplete; rerun its setup.', 503) from exc
        self.runner = OpenSCADAdapter(timeout=180)
        self._fingerprint = None
        self._stat = None
        # Validate both supported presets at startup without running the app or hardware.
        for profile in ('ender3v3ke-pla-04', 'ender3v3ke-petg-04'):
            self.profile_for(profile)

    def profile_for(self, profile_id):
        current = self.executable.stat()
        if self.compat:
            try:
                self.compat_digest = hashlib.sha256((self.compat / 'manifest.json').read_bytes()).hexdigest()
            except OSError as exc:
                raise ModelError('CREALITY_COMPAT_INVALID', 'Private runtime manifest changed or disappeared.', 503) from exc
        key = (current.st_size, current.st_mtime_ns, current.st_ino, self.compat_digest)
        if key != self._stat:
            self._fingerprint = hashlib.sha256(self.executable.read_bytes() + self.compat_digest.encode()).hexdigest()
            self._stat = key
        return resolved_profile(profile_id, self.presets, self._fingerprint)

    def _command(self, directory):
        command = [self.runner.prlimit, '--as=4294967296', '--cpu=181',
                   f'--fsize={MAX_GCODE_BYTES}', '--nofile=256', '--', self.runner.bwrap,
                   '--unshare-all', '--unshare-user', '--die-with-parent', '--new-session', '--cap-drop', 'ALL']
        for path in ('/usr', '/lib', '/lib64', '/bin', '/etc/ld.so.cache', '/etc/fonts'):
            if Path(path).exists():
                command += ['--ro-bind', path, path]
        command += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                    '--dir', '/slicer', '--ro-bind', str(self.runtime), '/slicer',
                    '--ro-bind', str(self.resources), '/slicer-resources']
        if not self.compat:
            executable_inside = Path('/slicer') / self.executable.relative_to(self.runtime)
            command += ['--ro-bind', str(self.resources), str(executable_inside.parent.parent / 'resources')]
        if self.compat:
            resource_alias = '/compat/' + str(self.loader.parent.parent.relative_to(self.compat) / 'resources')
            command += ['--ro-bind', str(self.compat), '/compat',
                        '--ro-bind', str(self.resources), resource_alias]
        command += [
                    '--dir', '/work', '--ro-bind', str(directory / 'model.stl'), '/work/model.stl',
                    '--ro-bind', str(directory / 'configs'), '/work/configs',
                    '--bind', str(directory / 'output'), '/work/output', '--chdir', '/work',
                    '--clearenv', '--setenv', 'HOME', '/tmp', '--setenv', 'LANG', 'C.UTF-8',
                    '--setenv', 'LC_ALL', 'C',
                    '--setenv', 'XDG_CONFIG_HOME', '/tmp/config',
                    '--setenv', 'LD_LIBRARY_PATH', '/slicer/bin:/slicer/usr/lib:/slicer/usr/lib/x86_64-linux-gnu:/slicer/usr/bin:/slicer/lib:/slicer',
                    '--setenv', 'SLIC3R_RESOURCES_DIR', '/slicer-resources', '--']
        if self.loader:
            command += ['/compat/' + str(self.loader.relative_to(self.compat)), '--library-path',
                        '/compat/usr/lib/x86_64-linux-gnu:/compat/lib/x86_64-linux-gnu:/slicer/bin:/slicer/usr/lib:/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu']
        command += ['/slicer/' + str(self.executable.relative_to(self.runtime)),
                    '--cli', '--slice', '0', '--need-gcode-file', '--debug', '1',
                    '--datadir', '/tmp/config', '--outputdir', '/work/output',
                    '--load-settings', '/work/configs/machine.json;/work/configs/process.json',
                    '--load-filaments', '/work/configs/filament.json', '/work/model.stl']
        return command

    def slice(self, directory):
        directory = Path(directory).resolve()
        try:
            bundle = json.loads((directory / 'profile.json').read_text())
            config_dir = directory / 'configs'
            config_dir.mkdir(mode=0o700)
            for kind in ('machine', 'process', 'filament'):
                (config_dir / (kind + '.json')).write_text(encoded(bundle[kind]))
            stdout, stderr = self.runner._run(self._command(directory), directory)
            # The CLI can exit successfully without slicing on affected releases.
            # Completion requires a real, bounded plain G-code file, never GUI startup.
            files = list((directory / 'output').glob('*.gcode'))
            if len(files) != 1 or files[0].is_symlink() or not 0 < files[0].stat().st_size <= MAX_GCODE_BYTES:
                raise ModelError('CREALITY_NO_OUTPUT', 'Creality Print did not produce exactly one bounded G-code file. No print is available.', 409)
            destination = directory / 'output/print.gcode'
            if files[0] != destination:
                files[0].replace(destination)
            return stdout, stderr
        except ModelError as exc:
            if exc.code in ('COMPILE_FAILED', 'COMPILE_TIMEOUT'):
                raise ModelError('CREALITY_SLICE_FAILED', 'Creality Print slicing failed; no print can start.', 409,
                                 stdout=exc.stdout, stderr=exc.stderr) from exc
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise ModelError('CREALITY_SLICE_FAILED', 'Creality Print input/output validation failed.', 409) from exc
