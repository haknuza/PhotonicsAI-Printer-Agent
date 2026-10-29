# Approved-model printing on the Ender-3 V3 KE

The dashboard has a separate print-job service: approved STL → local slicing →
exact-job review → final approval → explicit start. Printing is disabled by default.
Generation, compilation and model approval never upload, heat, home or move the
printer. The mock stdio specialist and positioning validator are unchanged.

## Prerequisites and startup

Use the normal Ender-3 V3 KE printing attachment, a configured 0.4 mm nozzle,
1.75 mm filament and saved `default` bed mesh. Check the actual nozzle/material;
firmware configuration cannot prove what is fitted. Keep other controls idle.

Besides the existing Linux OpenSCAD/Bubblewrap/prlimit setup, use the official
Creality Print Linux runtime. From the repository root:

```sh
python3 scripts/setup_creality.py --compat
```

This downloads the pinned official 7.3.0.6157 AppImage, verifies its published
SHA-256 and extracts it under ignored `.tools/creality/squashfs-root/`. No system
packages, printer firmware or user GUI profiles are changed. On Ubuntu 22.04,
`--compat` also extracts a private newer loader/C++/text-rendering library set
from signed Ubuntu packages and parallel Jammy Soup3/WebKit4.1 packages. Only the
Bubblewrap slicer process uses them; the operating system is not upgraded.
Newer supported systems may omit `--compat`. These scripts require an Ubuntu
APT environment. This compatibility bundle was verified on this workstation,
not across every Linux distribution. For an existing
complete download, use `--archive /absolute/path/to/the/AppImage`. The installer
refuses a checksum mismatch. An existing unrecognized runtime is not overwritten.

The adapter detects that private runtime automatically. For another extracted or
native installation, set `CREALITY_PRINT_BIN` to its actual native CLI executable
and `CREALITY_PRINT_RESOURCES` to its bundled `resources` directory containing
`profiles/Creality`. An AppImage must first be extracted. An explicit private
compatibility directory can be selected with `CREALITY_PRINT_COMPAT_DIR`. Invalid explicit settings
fail closed. PrusaSlicer is not used as a fallback. The bundled preset inheritance
is resolved locally, including the Ender-3 V3 KE machine, standard 0.20 mm process
and CR-PLA/CR-PETG filament presets. Their content and the executable are hashed
into each prepared job's provenance. Changed presets/runtime require a new job.

Start the authenticated SSH master in one terminal. Replace this documentation
address with the printer address, and enter the password locally:

```sh
SSH_HOST=192.0.2.10 photonics-printer-mcp-main/printerctl connect
```

Leave it open. Stop the previous dashboard and start a new backend from the repo
root in another terminal, preserving any desired AI provider settings:

```sh
lab-agents/.venv/bin/python lab-agents/app.py \
  --print-backend ssh --printer-host 192.0.2.10 --port 8770
```

Add `--prompt-groq-key` to privately re-enter a Groq key. An alternative master
socket uses `--ssh-control-path /absolute/path`. No passwords are stored by the
print adapter. Startup constructs it without contacting hardware. Printer
environment variables cannot enable printing. `/api/printer` remains mock-only;
actual readiness/progress lives under `/api/prints`.

## Dashboard workflow

1. Generate/compile, preview and approve the latest model revision.
2. Check the spool label and nozzle, then select PLA/0.4 mm or PETG/0.4 mm.
   No material is preselected. The vendor presets have explicit bounded overrides: 0.2 mm layers,
   15% infill, three perimeters, no supports and 40 mm/s general print speed.
   PLA uses 210/60 °C and PETG uses 240/80 °C nozzle/bed temperatures. Check
   against your spool. These are conservative settings, not physically qualified.
3. Click **Prepare approved model for printing**. It checks readiness and slices
   local snapshots without uploading or starting anything.
4. Review the model revision, material, settings, hash, estimated time, filament
   estimate and toolpath. Download the exact G-code if desired. The top view
   overlays extrusion paths across layers; long previews are labeled truncated.
   It is not a layer viewer or collision simulation. Orientation and scale are
   retained; the model is centered. The current size limit is 208 × 208 × 240 mm
   to allow skirt room; the resulting path is independently bounds-checked.
5. Confirm both exact-job review and actual material/nozzle, clear plate/path
   and regular-printing setup. Click **Approve job & start print**. This physical
   action homes the printer, loads `default` mesh, heats and extrudes. The file
   turns heaters off, lifts Z 5 mm, then returns to the already-homed X=0 / Y=0
   origin at 30 mm/s before disabling motors. Z stays raised above the finished
   part; it is not re-homed. The review shows this completion behavior, and the
   validator requires this exact ending and bounds-checks the lift/return moves.
   No camera check exists yet.
6. Use **Read print progress** for actual state, temperatures and file progress.
   Acceptance is not completion. Status is attributed to a job only when the
   filename matches. There is no background polling or unattended recovery.
   Stay for the first layer. Pause/cancel from the printer touchscreen; use its
   physical power switch when necessary.

Old prepared PrusaSlicer jobs cannot be approved after migration; prepare a new
Creality Print job. A changed model/profile/target requires a new relevant approval. Editing source
disables local print controls until a newly compiled revision is reviewed.
Server checks enforce the latest approved persisted model revision.
After updating completion behavior, restart the backend and prepare a fresh print
job. The changed settings invalidate approval of older prepared files. An active
or already uploaded print file is not changed by a software update.

## HTTP contract for the future chatbot host

These operations are separate from v1 motion stdio. Existing LAN authentication
protects them; POSTs require same origin and `X-Lab-Control: local`. Inputs have
closed fields. A host must keep approval/start behind its trusted operator UI;
model output cannot approve physical setup or supply G-code, credentials or profiles.

| Operation | Request |
| --- | --- |
| Availability and profiles | `GET /api/prints/config` |
| Real printer readiness | `GET /api/prints/printer` |
| Slice approved model | `POST /api/prints` with `model_job_id`, `profile_id` |
| Saved jobs | `GET /api/prints` |
| Local slicing/result | `GET /api/prints/<job_id>` |
| Exact G-code | `GET /api/prints/<job_id>/gcode` |
| Final approval | `POST /api/prints/<job_id>/approve` with `gcode_sha256`, `confirmed:true`, `setup_confirmed:true` |
| Explicit start | `POST /api/prints/<job_id>/start` with returned `token` |
| Real progress | `GET /api/prints/<job_id>/status` |

Slicing states: `queued`, `slicing`, `ready` or `error`. Approval returns a
single-use token expiring after 120 seconds. The UI immediately starts the reviewed
job with that token. Start persists `dispatching` before network I/O, followed by
`submitted`, `error` or `uncertain`. Progress can show `printing`, `paused`,
`completed`, `cancelled` or `error`. A timeout may mean the job started. Inspect
the printer; never blindly submit another copy. No automatic retry occurs.

## Execution boundaries and limitations

Creality Print runs inside mandatory Bubblewrap isolation with read-only model/resolved JSON profiles and vendor runtime,
writable output only, clean environment, no network, 180-second wall limit, 4 GiB
address-space limit, bounded logs and 16 MiB output limit. There is one worker,
at most two pending/running jobs and at most 100 stored jobs. No browser/provider
can choose arbitrary slicer arguments, scripts or settings.

The host validates current model approval and source/STL hashes, snapshot/settings,
output hash, selected printer target and configuration. G-code validation limits
explicit travel, temperature, feed and extrusion, rejecting commands outside the
fixed dialect. It does not certify fit, adhesion, overhangs, print quality or
physical clearance, including motion inside the firmware homing routine.

The separate remote helper reads the configured virtual SD directory. It checks
ready/idle/stationary/unpaused state, zero existing heater targets, configured
nozzle/filament, saved mesh, travel bounds, normal speed/extrusion overrides and
zero G-code offsets. It rechecks after upload and verifies the file hash. Files
have server-generated immutable names, with a durable launch marker written
before one fixed `SDCARD_PRINT_FILE FILENAME=...` command. No firmware or Moonraker
installation is performed. The existing motion lock excludes simultaneous
positioning submission; firmware print state prevents subsequent positioning.
Other clients/touchscreen actions still require operator coordination.

The native operation is documented by
[Creality's G-code reference](https://github.com/CrealityOfficial/Ender-3_V3_KE_Klipper/blob/main/docs/G-Codes.md),
[virtual SD implementation](https://github.com/CrealityOfficial/Ender-3_V3_KE_Klipper/blob/main/klippy/extras/virtual_sdcard.py)
and [API socket reference](https://github.com/CrealityOfficial/Ender-3_V3_KE_Klipper/blob/main/docs/API_Server.md).

Ignored jobs live under `prints/` inside the model store. Restart fails interrupted
slicing, discards approval tokens and marks interrupted/previously submitted work
uncertain pending inspection; it never replays a start. Uncertain jobs cannot be
approved again. Printer files/launch markers remain to prevent replay; archive
manually only after verifying no job is active.

## Checks

```sh
python3 scripts/check.py
RUN_SLICER_INTEGRATION=1 photonics-printer-mcp-main/.venv/bin/python \
  -m unittest discover -s printing_agent/tests -p test_print_jobs.py -v
photonics-printer-mcp-main/.venv/bin/python scripts/check_model_preview.py --print-fixture
```

Automated tests use fixture printers/SSH. Optional integration checks run real
local compilation/slicing with test-only printer responses. Operator-requested
real connection/readiness checks are recorded separately. No physical print was
started during implementation; fitted nozzle, filament, first layer and print
quality still require operator verification before the first real job.

The documented command-line interface is maintained in
[Creality Print's official repository](https://github.com/CrealityOfficial/CrealityPrint#command-line-slicing).
A successful CLI exit alone is not enough: the adapter requires exactly one
bounded plain G-code output and validates it. The adapter explicitly selects
`--cli --slice 0 --need-gcode-file` and supplies resolved JSON presets. A GUI-only launch, missing output
or unsupported G-code never becomes a runnable print job. OpenSCAD remains the
separate design-to-STL compiler.
