# PhotonicsAI Printer Agent

The intended product is a general-purpose conversational agent for a configurable
printer, delegated to by the main Photonics chatbot. Regular 3D printing is the
first tool mode; future attachments such as a laser will have their own modes.
Camera-assisted readiness checks will help determine whether the requested setup
is available or a human needs to change it. Tool-mode selection, camera inspection
and attachment setup workflows are planned. See the
[printer agent design and delivery scope](docs/PRINTER_AGENT_DESIGN.md).

A repository foundation for a PhotonicsAI specialist that will turn a chatbot
request into a parametric model, support design revisions and guide an approved
print job. The dashboard can ask Groq to turn a description into parametric
OpenSCAD, compile it locally to STL and preview each revision. The existing mock specialist and guarded printer infrastructure remain
independent; the full chatbot-to-print workflow is not implemented.

## What works now

- A replaceable AI provider layer with Groq support for descriptions, design
  clarification, parametric source generation and conversational revisions.
  Validated AI source goes through the same isolated OpenSCAD compiler.

- A dependency-free, mock-only specialist with versioned JSON-lines delegations,
  capability discovery, status and position reads.
- Guarded positioning/homing previews, separate operator approval, task-scoped
  single-use 120-second tokens, asynchronous motion-operation polling and stop.
- A dashboard with a .scad editor/file loader, asynchronous local OpenSCAD
  compilation, separate saved revisions, STL rotate/zoom preview, dimensions in
  millimeters, source/STL downloads and approval of a specific model version.
- Read-only mock printer status, position and capabilities. No scanner, camera,
  language provider or API key is needed for manual compilation or printer reads.
  AI generation requires a locally configured Groq key.
- Reusable SSH/Klipper transport, motion validation, watchdog and MCP interfaces,
  with hardware-free regression tests. Terminal motion defaults to mock mode.
- An opt-in Ender-3 V3 KE print-job adapter: isolated Creality Print, fixed PLA/PETG
  0.4 mm profiles, exact-job review/approval, one-shot SSH submission and progress
  reads. Settings have not been physically print-tested. See [printing setup](docs/PRINTING.md).

**Printing is disabled by default.** Model source can come
from Groq, your editor/file, or the clearly labeled hand-written example. The AI
may ask clarification questions; its assumptions and code need review. Model
approval never starts a print. Printer reads still use
a mock regardless of hardware-related environment variables. The stdio specialist
continues to expose only its existing mock positioning interface.

The motion controller requires an idle printer, zero heater targets and other
motion-specific preconditions. It excludes heating, extrusion and arbitrary G-code.
Actual printing uses the **separate print-job adapter**; the motion validator
does not accept print files.

## Install locally

Use Linux (tested on Ubuntu 22.04) with Python 3.10+, pip/venv, a native OpenSCAD
executable, Bubblewrap (`bwrap`) and util-linux (`prlimit`). Other operating systems
can use the mock specialist; model compilation currently fails closed outside Linux.
From the parent workspace:

```sh
git clone https://github.com/haknuza/PhotonicsAI-Printer-Agent.git
cd PhotonicsAI-Printer-Agent
python3 scripts/setup.py
```

If already in the repository root, omit the `cd` command. Setup creates ignored
component virtual environments and installs only the dashboard,
MCP and test dependencies. It starts no services or hardware connections and does
not install or reinstall OpenSCAD. Check the existing installation first:

```sh
command -v openscad
openscad --version
# Only if the existing executable is outside PATH, export its actual path:
export OPENSCAD_BIN=/usr/bin/openscad
```

Omit `OPENSCAD_BIN` to use PATH. An invalid explicit setting produces a clear
configuration error instead of silently selecting another executable. For optional
Ubuntu installation commands and isolation requirements, see [OpenSCAD setup and
execution boundary](docs/OPENSCAD.md). Compilation needs no slicer; optional
printing additionally requires [Creality Print](docs/PRINTING.md).

## Start in mock mode

The specialist uses only the standard library and can run even without setup:

```sh
python3 -m printing_agent --mode mock
```

Send one JSON request per line; keep the process open across related requests:

```json
{"version":1,"specialist":"3d-printing","task_id":"task-1","request":{"command":"describe"}}
{"version":1,"specialist":"3d-printing","task_id":"task-1","request":{"command":"status"}}
```

To start the dashboard after setup, from the repository root:

```sh
lab-agents/.venv/bin/python lab-agents/app.py --printer-mode mock --port 8770
```

Open <http://127.0.0.1:8770>. Stop with Ctrl+C. No other local service is required.
Load **hand-written example**, click **Compile new revision**, then rotate/zoom
the STL and inspect its dimensions (the example is 24 × 24 × 6 mm). Download the
.scad/.stl artifacts or explicitly approve that model version. Edit the source
and compile again to create a new, unapproved revision. No AI model generation
is implied by the hand-written example. A failed compile shows diagnostics and exposes no STL download.

Model jobs are stored in ignored `lab-agents/results/models/`, or the operator
setting `OPENSCAD_JOBS_DIR`. No `.env` file is loaded automatically: export settings
in the launching shell. Model source must be self-contained; external model assets
are denied. Compilation runs inside mandatory Bubblewrap isolation with resource
limits; a job directory alone is not a sandbox. See [the model API and limits](docs/OPENSCAD.md).
For optional password-protected LAN access, see [the dashboard README](lab-agents/README.md).

## Enable description-to-model generation with Groq

From the repository root, configure the key in the backend shell (not the browser):

```sh
export MODEL_PROVIDER=groq
export GROQ_MODEL=openai/gpt-oss-120b
lab-agents/.venv/bin/python lab-agents/app.py --prompt-groq-key --printer-mode mock --port 8770
```

Paste the actual Groq key at the hidden terminal prompt and press Enter. The key
is used only by this backend process and is not saved. Empty input stops startup.
Stop any earlier dashboard first; a busy port or model store now produces a clear
startup error. Wait for `AI configuration: ready` and `Running on…`, then open that
URL. The page shows a Backend PID matching the terminal to identify the process.
This local configuration check makes no Groq API request and does not authenticate
the key. An exported `GROQ_API_KEY` still works without the prompt flag.

Describe an object with dimensions/units and click **Generate model**. Answer any
design questions in the same box. Validated source is sent automatically to local
OpenSCAD, then shown with source, STL preview, millimeter dimensions and downloads.
Select that model and describe a change to create an unapproved revision. Review
assumptions and source before approval; generated geometry can still be wrong.

Descriptions and selected source are sent to Groq; API keys remain server-side
and are excluded from compiler environments and persisted request records. Missing
credentials disable generation only. There are no automatic paid retries or fake
fallback models. No live Groq call was verified during development because no key
was configured; offline transport/fixture tests and actual compilation were run.

See [generation setup, API, limitations and Photonics migration](docs/MODEL_GENERATION.md).
The provider protocol separates cloud generation from compilation/approval. Adding
Photonics requires its actual API contract and a provider adapter; it is not an
implemented switch yet. The stdio motion specialist still has no AI-generation tools.

To start the retained printer MCP server explicitly in simulation:

```sh
PRINTER_MODE=mock PRINTER_ALLOW_MOTION=0 FASTMCP_CHECK_FOR_UPDATES=off \
  photonics-printer-mcp-main/.venv/bin/python photonics-printer-mcp-main/fastmcp_server.py
```

This is a stdio server: an MCP host must communicate with it; it is not an HTTP
page. The dependency-free controller JSON process is also available:

```sh
photonics-printer-mcp-main/printerctl json --mock
```

For a complete simulated positioning preview/approval/execution/polling example:

```sh
python3 examples/router_mock_demo.py
```

The example supplies synthetic approval for mock positioning only. See
[router integration](docs/ROUTER_INTEGRATION.md), the
[specialist manifest](printing_agent/manifest.json),
[request schema](printing_agent/request.schema.json) and
[model instructions](printing_agent/SYSTEM_PROMPT.md). These describe an existing
local integration boundary; a PhotonicsAI chatbot/orchestrator is not included.

## Checks

```sh
python3 scripts/check.py
```

This runs the specialist, controller and dashboard suites, plus dependency checks.
Default tests use in-memory mocks, local sockets, stubbed SSH and fake compiler
processes. Real compiler and namespace checks are opt-in:

```sh
RUN_OPENSCAD_INTEGRATION=1 python3 scripts/check.py
# Optional installed Chrome/Chromium end-to-end viewer check:
photonics-printer-mcp-main/.venv/bin/python scripts/check_model_preview.py
# Offline AI fixture + real compiler/browser; no cloud call:
photonics-printer-mcp-main/.venv/bin/python scripts/check_model_preview.py --generation-fixture
```

No test uses a physical printer. See
[validation](docs/VALIDATION.md) for the checks actually run during cleanup and
[AGENTS.md](AGENTS.md) for development constraints. No real hardware test is
part of this cleanup.

## Structure

```text
printing_agent/                 Groq/provider boundary, design jobs, OpenSCAD jobs, mock specialist, tests
photonics-printer-mcp-main/      Guarded motion controller, SSH transport, MCP, tests, MIT license
lab-agents/                    Model editor/preview/API, mock status, local Three.js, LAN gate
examples/                      Mock motion demo and hand-written parametric_spacer.scad
scripts/                       Local setup, hardware-free checks and optional browser check
docs/                          Integration and validation notes
.github/workflows/             Retained test automation, fixed for this layout
```

The retained controller and dashboard directories keep their existing paths for
import and startup compatibility. The project now focuses on conversational printer
control, model generation, review and approved printing.

## Future workflow and approval boundaries

1. The PhotonicsAI chatbot delegates a model request through the orchestrator.
2. The specialist generates parametric OpenSCAD source and exports a model.
3. The user previews the model, requests revisions and gives **model approval**.
   This accepts the design; it does not authorize a printer job.
4. Beginner guidance and source-backed RAG help select appropriate printing
   choices. Verified printer/material profiles constrain slicer settings.
5. A slicer generates G-code and the agent presents the exact model revision,
   printer, material, settings and resulting job for **final print approval**.
6. A separate print-job adapter submits only the approved job and monitors it.
   Revisions or material/profile/job changes require renewed relevant approval.

Compilation/export of supplied .scad, STL previews, revision storage and model
approval and Groq-backed source generation/revision are implemented. Beginner guidance, verified profiles,
chatbot orchestration and RAG remain to implement. The dashboard now supports
slicing, separate final print approval, optional SSH submission and explicit
progress reads. These remain separate from the controller's motion-path approval.

**Next milestones:** verify live model generation, then add the tool-mode registry
and readiness/setup workflow described in the [agent design](docs/PRINTER_AGENT_DESIGN.md).
The Photonics chatbot team will connect the delivered component through an agreed
delegation contract. Camera inspection and physical qualification of the print
profiles remain future work. Model approval alone never authorizes printer operation.

## Attribution

The retained printer component is [MIT licensed](photonics-printer-mcp-main/LICENSE).
This standalone project derives from [PhotonicsAI Lab work](https://github.com/Arenananyan/PhotonicsAI-Lab-Cell-Erasing)
by Aren Ananyan, David Sargsyan,
Andranik Arakelyan and Yerem Bareghamyan. Existing printer license notices remain
with the component; this README does not relicense the rest of the repository.

The local STL viewer vendors Three.js r160, STLLoader and OrbitControls under
their [MIT license](lab-agents/static/vendor/three-r160/LICENSE); no CDN is required.
