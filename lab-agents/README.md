# PhotonicsAI Printer Agent dashboard

A Flask dashboard for Groq-backed description-to-OpenSCAD generation, design
clarification and revision, locally compiling `.scad` source and previewing STL
with rotation/zoom and millimeter dimensions, downloading exact source/STL versions
and approving a specific model revision. It also displays read-only in-memory mock
printer status, position and capabilities through the retained mock interface.

The separate [print-job adapter](../docs/PRINTING.md) uses Creality Print to slice approved models and
submits an explicitly approved job to the Ender-3 V3 KE. It is disabled by default;
enable it with `--print-backend ssh --printer-host HOST`.
The included parametric spacer is explicitly a hand-written example. Model approval never invokes any printer command.

From the repository root with Python 3.10+ on Linux, native OpenSCAD, Bubblewrap
and util-linux installed:

```sh
python3 scripts/setup.py
# Only if needed, set the actual path to the existing executable:
export OPENSCAD_BIN=/usr/bin/openscad
lab-agents/.venv/bin/python lab-agents/app.py --printer-mode mock --port 8770
```

Open <http://127.0.0.1:8770>. Load the hand-written example, compile a revision,
inspect the preview and approve that version explicitly. Edits require another
compilation and approval. Saved versions remain available in the revision list.
No scanner, camera or MCP subprocess is needed. Manual compilation and printer
reads need no language provider/key. AI generation requires a server-side Groq key;
start with `--prompt-groq-key` to enter it privately in the same backend process.
The key is not saved. Stop earlier dashboards first and compare the Backend PID
shown in the terminal/page if troubleshooting. See [generation setup and API](../docs/MODEL_GENERATION.md). The
app always constructs a mock motion specialist. The separate print-job adapter
requires its own explicit startup flags.

See [OpenSCAD setup, API and execution boundary](../docs/OPENSCAD.md) for executable
configuration, optional Ubuntu package installation, resource limits, external-file
restrictions, persistent job storage and remaining security limitations. A temporary
directory alone is not sandboxing; compiler isolation is mandatory and fails closed.
The application does not auto-load `.env` or auto-install OpenSCAD.

Printer routes remain read-only: `GET /api/config`, `GET /api/printer/describe`,
`/status`, `/position` and `/capabilities`. Model routes live at `/api/models`;
model POSTs require the existing `X-Lab-Control: local` convention and same origin.
Generation routes (`POST /api/models/generate`, `GET /api/models/generations` and
`GET /api/models/generations/<request_id>`) use the same authentication/header
conventions. Missing Groq credentials disable AI generation only. Print-job routes
at `/api/prints` use the same authentication and POST controls. Only an explicit
final job approval and start can submit a print. Failed model jobs expose diagnostics and
source but no STL. Model approval binds both source and STL hashes to a server-owned
revision ID. Restart fails interrupted compilations; it does not resume them.

## Optional local-network access

Append `--host 0.0.0.0`. Remote browsers need username `lab` and either the
operator-exported `LAB_REMOTE_PASSWORD` (12+ characters) or the generated password
in the ignored, owner-only `.remote-access-password` file in this directory.
Loopback connections with a loopback Host remain password-free; forwarded headers
cannot bypass the gate. The gate protects model APIs/downloads, printer reads and
static assets too. Plain HTTP Basic authentication is for a trusted local network;
public hosting requires separate deployment work.

## Checks

From this directory after root setup:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

From the root, `python3 scripts/check.py` includes adapter and revision tests.
`RUN_OPENSCAD_INTEGRATION=1` adds actual compiler/namespace tests. The optional
`scripts/check_model_preview.py` uses installed Chrome through the printer test
Python environment. Its `--generation-fixture` option verifies offline design
questions/generation/revisions with the real compiler, without a live AI call.
Its `--print-fixture` option checks real slicing and print controls using a test-only
printer fixture. All automated checks remain independent of physical devices.
