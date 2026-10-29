# OpenSCAD compilation and STL preview

This adapter compiles supplied `.scad` source. The dashboard also has a separate
[Groq description-to-model layer](MODEL_GENERATION.md) that generates source and
passes it through this same compiler. The compiler itself does not call AI services,
slice models, connect to printers or submit print jobs.
The included `examples/parametric_spacer.scad` is a hand-written verification
example, not AI-generated content.

## Supported host and setup

Compilation currently supports **Linux with native OpenSCAD, Bubblewrap and
util-linux**, tested on Ubuntu 22.04 with OpenSCAD 2021.01. Python is 3.10+.
The browser needs WebGL and JavaScript module/import-map support (tested in local
Chrome). Three.js r160, STLLoader and OrbitControls are served locally with their
MIT license; compilation/preview does not require a CDN or language-provider key.
Other operating systems can still use the existing mock motion specialist;
compilation fails closed there until an equivalent isolation adapter is added.

Check the existing installation first:

```sh
command -v openscad
openscad --version
command -v bwrap
command -v prlimit
```

If OpenSCAD is already installed outside PATH, configure its actual **executable
path**, including quotes around a path containing spaces:

```sh
export OPENSCAD_BIN=/usr/bin/openscad
# Example for a different existing native installation:
# export OPENSCAD_BIN='/opt/OpenSCAD/bin/openscad'
```

The optional `OPENSCAD_BIN` takes precedence over PATH. An explicitly invalid
value is an error, not a reason to silently pick another compiler. This setting
is an executable path/name, not a shell command or extra CLI arguments. The
supported installation is a native Linux binary using system runtime libraries;
AppImage/FUSE and application bundles with private relative dependencies are not
supported by this mount profile. No reinstall occurs automatically.

Only if a required package is actually absent, an operator on Ubuntu may use:

```sh
sudo apt update
sudo apt install openscad bubblewrap util-linux
```

The application never runs these commands or changes host security policy.
Bubblewrap must be allowed to create unprivileged user, mount, PID and network
namespaces. If the host/container blocks them, jobs fail with `SANDBOX_FAILED`
and diagnostics. Resolve the execution environment; there is no unsandboxed
fallback and no instruction to disable host safeguards.

From the repository root:

```sh
python3 scripts/setup.py
lab-agents/.venv/bin/python lab-agents/app.py --printer-mode mock --port 8770
```

Open <http://127.0.0.1:8770>. Settings must be exported in the launching shell;
`.env` is not loaded automatically. `lab-agents/.env.example` documents the
settings. `OPENSCAD_JOBS_DIR` optionally selects a private persistent job store;
default is ignored `lab-agents/results/models/`. One backend process may own a
store at a time (an advisory process lock enforces this).

## Dashboard workflow

1. Load the **hand-written example**, open a `.scad` file, or paste source.
2. Click **Compile new revision**. The dashboard shows submitting/queued/compiling,
   then success or the actual compilation error. Compiler stdout/stderr, including
   warnings, are available under diagnostics. A warning may accompany a successful
   STL; an error does not become success just because the process exits zero.
3. Inspect the STL with drag rotation, scroll/pinch zoom and **Fit view**. Dimensions
   show the original X/Y/Z bounding box in millimeters. The spacer is 24 × 24 × 6 mm.
   STL itself has no units; this pipeline interprets OpenSCAD coordinates as mm.
4. Download the exact `.scad` and `.stl` revision. A failed job can expose its source,
   but never its partial or invalid STL. If WebGL is unavailable, compilation and
   downloads still work; preview failure is reported and UI approval stays disabled.
5. Check the explicit model-only confirmation and approve that version. Editing
   clears the displayed preview and disables approval until another compile.
6. A revision has a new server-owned job ID, its own files and fresh approval state.
   Older versions remain selectable/downloadable. Their approvals remain historical,
   but cease to be current when a newer revision exists. Even an identical-source
   resubmission is a separate, initially unapproved revision.

Model approval records the job ID, revision and SHA-256 hashes of both source and
STL. Only the latest ready revision can receive current approval. It never calls
the printer controller. Future final print approval must separately bind a sliced
job, model version, verified printer/material profile and settings. Neither model
approval nor the existing motion-path token authorizes printing.

## HTTP API

The existing Flask backend and LAN authentication gate are retained. All model
POST requests require `Content-Type: application/json` and `X-Lab-Control: local`.
A supplied Origin must match the dashboard origin. The browser sends these headers;
the header is a local CSRF boundary, not user authentication. Remote access still
requires the existing LAN password gate; this is not a public multi-tenant service.

| Operation | Request |
| --- | --- |
| Compiler configuration/status | `GET /api/models/config` |
| Hand-written example source | `GET /api/models/example` |
| Saved job/revision history | `GET /api/models` |
| Submit source | `POST /api/models` with `{"source":"cube([20,20,10]);","filename":"model.scad"}` |
| New revision | Same POST plus `"parent_job_id":"SERVER_JOB_ID"` |
| Job status, diagnostics, hashes, dimensions, artifact URLs | `GET /api/models/SERVER_JOB_ID` |
| Exact source download | `GET /api/models/SERVER_JOB_ID/artifacts/model.scad` |
| Successful STL download | `GET /api/models/SERVER_JOB_ID/artifacts/model.stl` |
| Approve exact model version | `POST /api/models/SERVER_JOB_ID/approve` with `{"confirmed":true,"source_sha256":"FROM_STATUS","stl_sha256":"FROM_STATUS"}` |

Submission returns 202 with the job object. Poll `status` until `ready` or `error`;
intermediate states are `queued` and `compiling`. Artifact URLs are supplied in
`artifacts.source` and `artifacts.stl`. `error` contains a stable code and message;
`stdout`/`stderr` retain bounded diagnostics. Successful jobs include `dimensions_mm`,
triangle count and hashes. Queue/store saturation returns 429; missing compiler
configuration returns 503. Unknown IDs/artifact names return 404; premature or
changed artifacts, stale approvals and mismatched version hashes return 409.

Example request (server running):

```sh
curl -sS http://127.0.0.1:8770/api/models \
  -H 'Content-Type: application/json' -H 'X-Lab-Control: local' \
  --data '{"source":"cube([20,20,10]);","filename":"model.scad"}'
```

IDs, filenames on disk and paths are server-owned. A client filename is validated
metadata only; it cannot choose an output path or compiler arguments. Artifact
routes accept only the two fixed names for a validated hexadecimal job ID and
check containment, symlinks, regular-file status and hashes before returning data.
The stdio specialist's existing motion protocol is unchanged; these operations
are available through the dashboard HTTP API, not through `printer_command`.

## Untrusted-source execution boundary

A temporary/job directory **is not a sandbox**. This adapter uses several distinct
controls:

- Source is limited to 128 KiB of UTF-8. The current model-asset allowlist is
  **empty**: `include`, `use`, `import`, `surface`, deprecated import aliases and
  DXF file-reading functions are rejected before dispatch. Comments and string
  literals are tokenized separately; arbitrary identifiers cannot smuggle these
  operations past simple string/comment stripping. Non-ASCII syntax outside
  strings/comments is rejected conservatively. User asset uploads, OpenSCAD
  libraries and computed external paths are not supported in this version.
- Bubblewrap supplies a separate user/mount/PID/network namespace with dropped
  capabilities and a new session. It mounts OS runtime trees (`/usr`, `/lib`,
  `/lib64`, `/bin` where present), system font configuration and loader cache
  read-only. Only the selected compiler executable and this job's source are added.
  Host home directories, credentials, unrelated jobs and the host network are not
  mounted/shared. The source is read-only; only this job's output directory and an
  ephemeral `/tmp` are writable. The compiler inherits no host secrets or SSH state.
- The effective compiler invocation is `openscad -o output/model.stl model.scad`
  inside that namespace. Fixed subprocess argument lists use `shell=False`; no
  source content becomes command-line options. No arbitrary CLI arguments or
  automatic make-command execution are exposed.
- There are two compilation workers and at most eight pending/running submissions.
  A wall timeout of 30 seconds kills the process group; Bubblewrap's PID namespace
  and parent-death behavior contain compiler children. `prlimit` adds 2 GiB address
  space, CPU time (31 seconds by default), 32 MiB per-file and 128 file-descriptor
  limits. Diagnostic collection is capped at 64 KiB; overflow fails the job.
- Nonzero exit, explicit error diagnostics, absent/empty/oversized output, invalid
  or nonfinite geometry and meshes above 200,000 triangles fail accurately. STL
  output is checked and measured before becoming available. Files and approval
  hashes are immutable per revision at the application level.

Remaining limitations: this is a Linux application isolation profile, not a VM
or a proof against compiler/kernel vulnerabilities. OS runtime/font files remain
visible; the lexical deny policy is an additional conservative restriction, not a
complete security parser. Limits apply per process/file rather than to an entire
cgroup or filesystem quota, so a compromised compiler could still exhaust host
resources. Job IDs are correlation values, not per-user authorization. The local
store and configured compiler/tools must be owned/trusted by the operator; hostile
same-user processes or on-disk tampering are outside that trust boundary. Do not
expose the service as a public code-execution endpoint without further deployment
isolation, authentication, storage quotas and security review. Mesh preview and
model approval do not certify manifold geometry, printability or physical safety.

Metadata persists atomically. Restart marks interrupted jobs as failed and never
automatically resumes compilation. There is a maximum of 100 retained jobs in a
store; no automatic deletion discards revisions or approval history. Archive/remove
old jobs only with the backend stopped. Retention/quotas beyond this cap remain
future deployment work.

## Verification

```sh
python3 scripts/check.py
RUN_OPENSCAD_INTEGRATION=1 python3 scripts/check.py
photonics-printer-mcp-main/.venv/bin/python scripts/check_model_preview.py
```

The default suites use explicit fake compiler fixtures; their outputs are not
presented as actual OpenSCAD results. Opt-in tests use the real installed compiler,
compile the spacer, check actual syntax failure/warnings and probe the OS file,
source-write, environment and network-namespace boundaries. The optional browser
check uses installed Chrome/Chromium (`CHROME_BIN` can select it), the real compiler,
WebGL rendering, rotate/zoom, downloads, model approval, revised dimensions and
error display. It writes a preview screenshot under `/tmp` by default. Its optional
`--no-browser-sandbox` is only for constrained local test containers and does not
relax compiler isolation; it is not needed for the tested host.

See [validation results](VALIDATION.md) for checks actually run. The isolation and
viewer APIs follow [Bubblewrap's documented controls](https://github.com/containers/bubblewrap),
[OpenSCAD CLI export](https://files.openscad.org/documentation/manual/Using_OpenSCAD_in_a_command_line_environment.html),
[STLLoader](https://threejs.org/docs/pages/STLLoader.html) and
[OrbitControls](https://threejs.org/docs/pages/OrbitControls).
