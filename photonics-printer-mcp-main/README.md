# Photonics Printer MCP

Reusable guarded positioning infrastructure for the PhotonicsAI PhotonicsAI Printer Agent.
The controller includes a mock printer, SSH/Klipper transport, a remote watchdog,
structured JSON commands and stdio MCP tools. It was designed for an Ender-3 V3 KE
with firmware V1.1.0.17 and a Python 3.8 printer runtime; another printer requires
its own transport and policy review. Host Python is 3.10+.

**This is a motion controller, not a print-job interface.** It requires idle
state and zero heater targets and excludes heating, extrusion and arbitrary G-code.
A separate future adapter must validate and submit print jobs. Do not relax these
motion guards to implement printing. No real hardware may be used for cleanup or
automated development checks; see [AGENTS.md](AGENTS.md).

## Install and run in mock mode

From the repository root:

```sh
python3 scripts/setup.py
photonics-printer-mcp-main/printerctl status --mock
photonics-printer-mcp-main/printerctl json --mock
```

Or install just this component from its directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
PRINTER_MODE=mock PRINTER_ALLOW_MOTION=0 FASTMCP_CHECK_FOR_UPDATES=off \
  .venv/bin/python fastmcp_server.py
```

`requirements.txt` contains the MCP runtime dependency; `requirements-dev.txt`
adds explicit schema-test dependencies. The old macOS environment export was
removed; it is not the dependency specification for this checkout.

The terminal runner, JSON runner and MCP server default to mock mode. The
terminal runner requires explicit `--real` to select SSH, even when hardware
variables are set. The specialist and dashboard can only construct mock printers.

`simple.py` remains the operator-editable relative sequence; importing it never
runs the sequence. Its retained steps are positioning examples, not a model or a
print file. The shell wrapper resolves source paths relative to its own location.

## Interfaces and transport

See [JSON interface and examples](docs/LLM_INTERFACE.md),
[JSON schema](docs/printer-interface.schema.json) and
[model instructions](docs/LLM_PROMPT.md). The root specialist wraps these with
trusted-router approval and task ownership; see
[router integration](../docs/ROUTER_INTEGRATION.md).

| MCP tool | Purpose |
| --- | --- |
| `printer_status()` | Read state and estimated firmware position |
| `printer_command(request)` | Structured status, capabilities, positioning, homing, stop and motion-operation polling |
| `preview_sequence(steps, speed_mm_s=20, z_speed_mm_s=20)` | Validate a complete relative sequence; x/y/z scalar or simultaneous xy legs |
| `preview_position(x, y, z, speed_mm_s=20)` | Validate one straight absolute XYZ positioning operation |
| `execute_sequence(token, operator_confirmed=False)` | Execute a reviewed preview once after explicit confirmation |
| `emergency_stop()` | Dedicated shutdown path independent of the motion lock |

For an MCP host, set the absolute component `.venv/bin/python` as the command and
absolute `fastmcp_server.py` as its argument, with `PRINTER_MODE=mock` and
`PRINTER_ALLOW_MOTION=0`. Startup opens no hardware connection in mock mode.

The retained hardware transport uses an operator-established shared authenticated
SSH master, `SSH_HOST` and `SSH_CONTROL_PATH`. It does not store passwords or
silently fall back to another authentication method. `.env.example` has only a
documentation address and shell placeholders; `.env` is ignored and never loaded
automatically. `printerctl connect` is an explicit hardware entry point, outside
mock startup. Real MCP motion remains behind the existing local
`PRINTER_ALLOW_MOTION` gate and per-preview operator confirmation; this cleanup
does not enable it. Real homing defaults to manual/touchscreen operation.

Terminal mesh-clear/load and fixed XY-entry helpers remain operator actions.
Their confirmations are preserved. Explicit hardware terminal calls require
`--real`, including shutdown; `printerctl mesh-load PROFILE --real` preserves
that option. Never issue any of these against hardware as a software check.

## Preserved safeguards

- Complete-plan validation includes requested and 0.001 mm rounded coordinates.
  Travel bounds come from reported firmware axis minimum/maximum; negative
  coordinates within those bounds are valid. Missing/invalid bounds fail closed.
  Bounds and starting position are checked again at dispatch and during execution.
- Absolute motion requires all axes homed. Relevant relative/XY entry operations
  retain their own homing and fixed-Z checks. Firmware coordinates never prove
  physical path clearance. No implicit obstacle avoidance or coordinate reset.
- Default X/Y/Z speeds are 20 mm/s with a 30 mm/s ceiling, further constrained by
  verified firmware velocity/acceleration limits. Speeds above 20 require verified
  firmware settings. The controller never rewrites firmware limits.
- Idle state, heater targets, offsets, speed overrides, homing, mesh policy,
  estimated live/queued positions, velocity, timestamps, deadlines and final
  completion are checked. Dispatch and watchdog checks remain in place.
- The documented startup quirk allows exact zero live XYZ with zero velocity to
  be treated as missing telemetry for **at most 0.5 seconds** after dispatch.
  Other checks continue. The window closes on the first nonzero position/velocity
  report and cannot reopen. Preflight and final checks stay strict. At 30 mm/s,
  travel during the uncertain interval can reach 15 mm plus stopping latency.
- Status-bound arithmetic tolerance is 1e-9; fixed/planned Z tolerance is 0.002 mm.
  Watchdog polling is approximately 100 ms, firmware position refresh may be
  around 250 ms, and missing-response timeout is 1 second. Firmware estimates
  cannot detect every collision, skipped step or controller fault.
- Single-use preview tokens expire after 120 seconds. Changed plans, starts or
  bounds require a fresh preview. Asynchronous acceptance is not completion;
  poll the operation. Busy movement is rejected, never queued.
- Uncertain motion is never automatically retried. The watchdog attempts shutdown
  on faults; a separate stop path handles uncertain transport outcomes. Failed
  stop delivery may leave queued moves running. Emergency stop is shutdown,
  not pause/resume. Manual inspection/recovery is required.
- The runner lock excludes other copies, not touchscreen actions or unrelated
  clients. Keep other controls idle during authorized operation. The optional
  inspected firmware-homing routine has a configuration fingerprint, a 5 mm Z
  lift, 80–100 mm/s XY legs and mesh changes; its special path requires separate
  review and approval. It is not enabled by cleanup, setup or mock startup.

No new physical test or clearance validation was performed. Historical hardware
observations do not establish current positions, readiness or physical safety.

## Checks and license

From this component directory after setup:

```sh
PRINTER_MODE=mock PRINTER_ALLOW_MOTION=0 FASTMCP_CHECK_FOR_UPDATES=off \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

Tests use mock printers, local Unix sockets, stubbed SSH and in-process/stdio MCP
clients. The root [CI workflow](../.github/workflows/tests.yml) runs the retained
suites. See [validation](../docs/VALIDATION.md) for actual cleanup results.

[MIT license](LICENSE). Preserve its attribution notices with the component.
