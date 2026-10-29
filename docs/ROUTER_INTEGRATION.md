# Router integration: PhotonicsAI Printer Agent v1

The broader target is a general-purpose printer agent with attachment modes and
camera-assisted setup checks; see [the intended architecture](PRINTER_AGENT_DESIGN.md).
The `3d-printing` identifier and interface below describe the current v1 mock
motion contract. Tool-mode selection and camera readiness are not implemented
in this contract. Its `mock` mode describes the execution backend.

This repository provides a specialized printer delegate for a router-specialist
system. No existing router implementation or contract was present, so this
defines a small versioned interface that can be wrapped by a future router.
The existing printer controller supplies validation and simulated positioning
execution. The dashboard adds a separate [HTTP model compilation API](OPENSCAD.md)
and read-only mock printer status. The dashboard generation layer can use Groq through its separate
[description-to-model API](MODEL_GENERATION.md). Those model operations are not
part of this stdio motion contract; they never dispatch printer commands.
The dashboard's separate [approved print-job API](PRINTING.md) now supports
local slicing and explicitly enabled shared-SSH submission after final job approval.

## Launch and discovery

Run from the repository root with Python 3.10 or newer:

```sh
python3 -m printing_agent --mode mock
```

There are no additional dependencies, API keys, scanner processes or camera
requirements. The process uses stdin/stdout JSON lines: one request per line,
one response per line, with stdout reserved for protocol output. Keep one process
open for previews, execution and job polling. EOF waits for accepted simulated
work. Restart clears tokens, jobs and simulated state; it never resumes an action.
Requests are limited to 65,536 UTF-8 bytes including the newline. Blank lines are
ignored; malformed/oversized input produces an error and the next line can proceed.

The new entry point constructs only `MockPrinter`, regardless of `PRINTER_MODE`,
`PRINTER_ALLOW_MOTION`, `PRINTER_HOMING_MODE` or SSH settings. `--mode real` is
rejected. The retained controller has explicit hardware entry points outside
this mock-only workflow; terminal positioning now defaults to mock and requires `--real` for SSH.

Register [manifest.json](../printing_agent/manifest.json) with your router and
use [SYSTEM_PROMPT.md](../printing_agent/SYSTEM_PROMPT.md) for the specialist's model
instructions. The model proposes command objects; the host runs this adapter.
This is a deterministic execution boundary, not a separate hosted LLM or a
natural-language parser. Your router supplies language interpretation and delegation.

```json
{"version":1,"specialist":"3d-printing","task_id":"task-42","request":{"command":"describe"}}
```

`describe` reports static routing scope; `capabilities` reads dynamic mock printer
limits. Supported commands are `status`, `position`, `capabilities`, `move`, `home`,
`job`, `stop`, and `describe`. Other commands fail closed. Through this stdio interface, OpenSCAD
compilation, model previews, slicing, file upload,
print execution, heating and extrusion are unsupported. This adaptation provides
the controller foundation for a printing specialist; it cannot produce a part.

## Request and response

Each request has exactly `version:1`, `specialist:"3d-printing"`, a nonblank string
`task_id` (at most 128 characters), and `request`. The optional `operator_approval`
is accepted only with execution. Unknown fields and duplicate JSON keys are rejected.
The [request schema](../printing_agent/request.schema.json) describes this envelope.

`request` reuses the bounded printer v1 commands from
[LLM_INTERFACE.md](../photonics-printer-mcp-main/docs/LLM_INTERFACE.md), with two
differences: `describe` is added, and `operator_confirmed` is forbidden. The
adapter derives controller confirmation only from the separate approval object.
All coordinates are absolute printer millimetres; speed is mm/s, default 20,
maximum 30 subject to the unchanged controller policy. Moves are straight lines,
with no implicit Z lift or avoidance. `home` is firmware homing, with no application-defined observation position.

Responses have `version`, `specialist`, `task_id`, `ok`, `simulated:true`, `status`,
`result`, and `error`. For `describe`, `result` is the manifest. Other results
retain the complete printer response, including diagnostics, in `result`.
Adapter errors have `result:null`; controller failures retain the failed response.
Errors always include `code`, `message`, `retryable:false`, and `details`.

| Specialist status | Meaning |
| --- | --- |
| `completed` | Read-only request finished, or polled operation completed |
| `awaiting_approval` | Validated plan available, no movement dispatched |
| `running` | Accepted operation or running poll; poll its job ID |
| `stopped` | Emergency shutdown or operation stopped; inspect any error |
| `error` | Invalid request, controller rejection, or failed operation |

## Preview, approve, execute, poll

Preview:

```json
{"version":1,"specialist":"3d-printing","task_id":"task-42","request":{"command":"move","target":{"x":110,"y":120,"z":30},"speed_mm_s":5,"preview":true}}
```

At `awaiting_approval`, render `result.data.plan` and its simulation label. The
token is at `result.data.token`. It expires after 120 seconds and is single-use.
After the operator reviews the exact path and clearance, the trusted router UI
adds both approval booleans on this execution request:

```json
{"version":1,"specialist":"3d-printing","task_id":"task-42","request":{"command":"move","target":{"x":110,"y":120,"z":30},"speed_mm_s":5,"preview":false,"token":"TOKEN_FROM_PREVIEW"},"operator_approval":{"confirmed":true,"clear_path":true}}
```

The outer `running` response contains `result.data.job_id`. Poll:

```json
{"version":1,"specialist":"3d-printing","task_id":"task-42","request":{"command":"job","job_id":"JOB_ID_FROM_EXECUTION"}}
```

Use the same task ID through the lifecycle. Cross-task use of a token or job
returns `TASK_MISMATCH`. An optional command `id` correlates individual requests;
neither ID deduplicates commands. Never blindly resend execution requests. An
expired/used token, changed target/speed/start state, controller fault or uncertain
outcome must be reported. No automatic action retry occurs. Additional motion
while busy is rejected rather than queued. Status and emergency stop remain usable.

Homing follows the same preview/token/approval/job protocol using `command:"home"`
and optional `axes:["x","y","z"]`. Nothing homes on startup or during a read.
Emergency shutdown is an explicit request:

```json
{"version":1,"specialist":"3d-printing","task_id":"stop-1","request":{"command":"stop"}}
```

Stop invalidates pending previews globally. It is not a pause/resume operation.

## Trust boundary and verification

The subprocess is intended for one trusted router per printer. Task scoping
prevents accidental cross-task execution; task IDs are not authentication and
status can reveal the current active job ID. The trusted router must keep the
approval object out of model-generated fields and associate operator approval
with the exact displayed token/path. This local protocol does not authenticate
humans or expose an HTTP service.

Run the complete subprocess example and specialist regression suite:

```sh
python3 examples/router_mock_demo.py
python3 -m unittest discover -s printing_agent/tests -v
```

The example supplies **synthetic operator approval for mock execution only**.
Tests cover task isolation, approval requirements, token expiry/reuse, changed
plans/starts, bounds, busy behavior, stop, malformed JSON, environment isolation,
and subprocess framing. They cannot establish physical clearance or print quality.
The original printer suite remains documented in its own README and AGENTS.md.

## Future printing approvals

The current preview/token approval authorizes only a mock positioning or homing
operation. It is neither model approval nor final print approval. The dashboard now tracks model revisions and obtains design approval through
its separate model API. A future chatbot/orchestrator must integrate that boundary
and the separate [print-job API](PRINTING.md), obtaining approval for the exact
sliced job and confirming the actual material/setup. Its starting profiles still
need physical qualification. The current controller's idle/zero-heater motion
guards stay intact; printing is not exposed through this motion stdio contract.
