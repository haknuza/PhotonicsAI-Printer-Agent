# Validation

## Groq HTTP 403 transport fix — 29 October 2026

A controlled diagnostic to the public model-list endpoint, without any API key,
reproduced HTTP 403/error 1010 with Python urllib's default header. The same
unauthenticated request using `User-Agent: PhotonicsAI-3D-Agent/1.0` reached Groq
and received HTTP 401 with `invalid_api_key`, as expected when no key is supplied.
No model generation or paid inference call was used by the diagnostic.

The actual Groq provider now sends that descriptive application header. It also
distinguishes recognized gateway error 1010 from Groq's documented organization
and project model-permission codes. Unknown HTTP 403 responses no longer claim
that the key was authenticated or necessarily invalid. Raw bodies/keys remain
redacted, error reads are bounded, and no automatic model fallback/retry was added.

Provider/generation regressions: **17 passed**, including request headers, 403
code classification, oversized/malformed error bodies, key redaction and
single-attempt behavior. Dashboard/API/startup regressions: **25 passed**.
A startup test initially encountered the running user backend's model-store lock;
it was corrected to use its own temporary store and close its resources, without
touching or stopping that backend. Actual
user-key authentication and successful live generation remain unverified here.
The user's existing failed request is not replayed automatically; they must restart
the backend and explicitly submit again. No printer code/policy was changed.


## Groq key startup troubleshooting — 29 October 2026

Added `--prompt-groq-key` so the backend reads the key privately itself. Startup
rejects empty/noninteractive/unhidden key input and checks for an occupied address
before requesting a key. Port and model-store conflicts have explicit restart
messages. The terminal and model-config API/page expose the same Backend PID;
configuration messages no longer repeat the manual-compilation notice.

- Dashboard/API/startup suite: **25 passed**, including eight new startup cases.
- A real pseudo-terminal test entered a synthetic key, started the real dashboard
  and confirmed AI configuration ready and matching process PID over loopback.
  The key appeared in neither terminal output, HTTP responses nor local files.
- This verifies key loading only. No real user key, Groq authentication/request or
  printer connection was used. The browser/backend reported by the user could not
  be reached from the agent environment, so an old process was not assumed proven.

Existing environment-based startup remains supported. The prompt option is
explicit, memory-only and Groq-only; manual compilation needs no key. Docs now
prefer the short prompt option over shell-export and Python one-liner workarounds.


## Groq description-to-model integration — 29 October 2026

The retained regression suites passed with real compiler checks enabled: **214
passed, no skips/failures** (53 specialist/adapter/provider/generation tests, 144
printer tests and 17 dashboard/API tests). Commands and boundaries:

| Check | Result |
| --- | --- |
| `RUN_OPENSCAD_INTEGRATION=1 python3 scripts/check.py` | All retained suites plus dependency checks passed |
| Offline Groq HTTP transport tests | Correct endpoint/auth/JSON mode, response limits, malformed/truncated/tool output rejection, safe error/rate-limit handling, no retries |
| Generation lifecycle tests | Description/source → compile job, clarification context/six-turn cap, queue backpressure, provenance, fresh approval, stale revision rejection, explicit compiler-error repair, draft recovery and restart behavior |
| Dashboard/API tests | Generation submission/status/history, missing-key behavior, closed request fields, control-header/origin checks and no printer dispatch |
| Chrome with `--generation-fixture` | Scripted provider questions → real isolated SCAD/STL → preview/dimensions → explicit approval → revised model with fresh approval; unsafe source rejected |
| Normal keyless Chrome startup | Clear key configuration guidance; generation disabled; manual compile/preview/download/approval still usable |
| Syntax, JSON, local links and diff whitespace | Passed |
| Core printer files | Transport, commands, validator, watchdog, MCP server and operator sequence remain unchanged |

The full checks used mocks/stubbed SSH/local sockets for printer tests. Real
OpenSCAD checks compiled local models inside mandatory Bubblewrap isolation; they
never operated hardware. The two browser commands were:

```sh
photonics-printer-mcp-main/.venv/bin/python scripts/check_model_preview.py --generation-fixture
photonics-printer-mcp-main/.venv/bin/python scripts/check_model_preview.py
```

The provider fixture is test-only and labeled as scripted output, not a live AI
model. The normal app has no fake provider fallback. A first browser test clicked
approval before asynchronous preview/history loading had finished; its waits were
corrected and the rerun passed. Compiler output and rendering were real in those
checks; provider output was not produced by Groq.

**No live Groq request was made:** neither `GROQ_API_KEY` nor a local dashboard
`.env` was configured at inspection. The key was not requested through chat, read
from unrelated files or added to the repo. Configure it locally and submit an
actual description to validate model quality/account permissions/quota. The
Photonics adapter remains pending its real API contract; the replaceable provider
boundary is implemented, not a fabricated endpoint.

Generation credentials are backend-only, excluded from compiler environments,
requests/provenance and browser responses. Requests store design inputs/source
privately in the ignored model store. Approval still binds one exact source/STL
revision and cannot authorize printer action. No slicing, upload, heating,
extrusion, firmware changes or real printer connection was added or attempted.
Existing user/cleanup/OpenSCAD changes were preserved on the current branch; no
commits, pushes, stashes or resets occurred. Starting edited files were snapshotted
outside the repository at `/tmp/photonics-before-groq`.

See [generation setup and limitations](MODEL_GENERATION.md). Earlier results below
are historical and do not describe the current feature set.

## OpenSCAD integration — 28 October 2026

The following checks apply to the model compilation/preview integration. Earlier
cleanup results below are historical; they do not describe the current feature set.

| Check actually run | Result |
| --- | --- |
| Full retained suites with `RUN_OPENSCAD_INTEGRATION=1 python3 scripts/check.py` | **195 passed, no skips or failures**: 37 specialist/adapter, 144 controller, 14 dashboard/API |
| Final affected specialist/adapter rerun after parser/resource hardening | 37 passed, including real compiler and namespace checks |
| Real installed OpenSCAD | `/usr/bin/openscad`, version 2021.01; no installation/reinstallation |
| Hand-written spacer compilation | Actual STL: **24 × 24 × 6 mm**, 512 triangles |
| Actual syntax error and warning cases | Invalid source failed; usable geometry with a warning remained successful and preserved diagnostics |
| OS-boundary probe | Outside host file hidden, source read-only, secret environment removed, network namespace distinct |
| Chrome/WebGL end-to-end check | Passed source/example loading, real compile, rendered STL, dimensions, rotate/zoom, both downloads, model approval, revision reset and error state |
| Both component `pip check` calls | No broken requirements |
| Python syntax/JSON, local Markdown links, diff whitespace | Passed |
| Core printer files versus starting tracked content | Transport, commands, policy, watchdog, MCP and operator sequence unchanged |

The final browser command was
`photonics-printer-mcp-main/.venv/bin/python scripts/check_model_preview.py`,
using installed Chrome with its normal browser sandbox and software WebGL. Its
preview screenshot is `/tmp/photonics-openscad-preview.png`; test jobs/browser
profiles were temporary. All viewer assets are local, pinned Three.js r160 files
with the original MIT license. The second spacer revision changed height to 9 mm,
rendered as 24 × 24 × 9 mm and required new approval. The browser checked actual
view changes for rotation and zoom, not merely a canvas element.

Default compiler tests use explicit fake compiler fixtures; their output is never
presented as real OpenSCAD compilation. The two real compiler/namespace tests are
opt-in and skipped when `RUN_OPENSCAD_INTEGRATION` is unset. The full run above
enabled them. Adapter coverage includes executable discovery/configuration, file
policy, missing/empty/invalid output, nonzero and zero-with-error exits, timeout,
bounded diagnostics/concurrency/queue, artifact IDs/paths/symlinks/hashes, revision
approval and restart behavior. Model API tests assert no printer dispatch occurs.

The Codex nested sandbox initially blocked Bubblewrap network-namespace setup.
Real integration/browser checks succeeded outside that nested sandbox while the
application still enforced its own mandatory Bubblewrap compiler isolation. No
compiler fallback, host security setting change or printer access was used.

Supported model compilation is Linux only. Native OpenSCAD with system libraries
was verified; AppImage/FUSE/private runtime bundles, other operating systems,
external model assets and a multi-tenant deployment were not tested/supported.
Model-source and OS security limitations are documented in [OPENSCAD.md](OPENSCAD.md).
AI generation, orchestration, RAG, slicing and print control remain unimplemented.
No physical printer was contacted or operated.

The existing cleanup/user changes remained in place on `cleanup/3d-printing-agent`.
A small snapshot of files edited for this integration was kept outside the repo at
`/tmp/photonics-before-openscad`. No commits, pushes, resets or stashes were made.

## Repository cleanup — 28 October 2026

Checks ran on Linux with Python 3.10.12 against the cleaned repository. The root
setup script created fresh ignored component virtual environments and installed
Flask 3.1.3, FastMCP 4.1.0 and the declared test requirements. No OpenSCAD, slicer,
scanner, camera service or language provider was installed or started.

| Check actually run | Result | Scope |
| --- | --- | --- |
| `python3 scripts/setup.py` | Passed after network-enabled retry | Both component installations and dependency checks |
| Specialist suite | 17 passed | Protocol/schema, trusted approval, task ownership, expiry/reuse, changed plans/starts, bounds, busy/stop and uncertain outcomes |
| Printer suite | 144 passed | Preserved motion policy/watchdog, firmware bounds, mock default, confirmation, local sockets, stubbed SSH, JSON and in-process/stdio MCP |
| Dashboard suite | 10 passed | Read-only routes, mock isolation, honest scope/assets, controller errors and generic LAN authentication |
| Both virtual-environment `pip check` calls | Passed | No broken requirements |
| Documented dashboard startup over loopback | Passed | Port 8770, HTML, JS/CSS/logo and all four mock read endpoints |
| Documented specialist startup | Passed | JSON-lines mock status response |
| Terminal status without a mode flag | Passed | Simulated status, no implicit SSH selection |
| Python syntax/import checks | Passed | Retained sources; all controller modules import in mock mode |
| Remote Python 3.8 grammar checks | Passed | `printer_policy.py` and `printer_watchdog.py` |
| JavaScript and shell syntax | Passed | `node --check lab-agents/static/ui.js`; `sh -n photonics-printer-mcp-main/printerctl` |
| Markdown links, stale-path review and `git diff --check` | Passed | No broken local links or references requiring removed services |

The complete retained run is **171 tests, no failures or skips**. Run it again
from the root after setup:

```sh
python3 scripts/check.py
```

Equivalent individual suites:

```sh
photonics-printer-mcp-main/.venv/bin/python -m unittest discover -s printing_agent/tests -v
(cd photonics-printer-mcp-main && PRINTER_MODE=mock PRINTER_ALLOW_MOTION=0 FASTMCP_CHECK_FOR_UPDATES=off .venv/bin/python -m unittest discover -s tests -v)
(cd lab-agents && .venv/bin/python -m unittest discover -s tests -v)
```

The specialist suite also runs `examples/router_mock_demo.py` in a subprocess.
Its approval is a synthetic mock-only fixture, not approval for a physical job.
The controller suite intentionally tests rejected motion requests; expected MCP
error logs during those negative cases are not test failures.

## Environment issues encountered

The first setup attempt could not resolve the package index inside the restricted
network sandbox. Retrying with network access completed successfully. The first
sandboxed full test attempt stalled during FastMCP teardown and hit its 90-second
limit. The same complete check command then passed outside the sandbox with mock
mode, motion disabled and update checks off. No dependencies or verification
remain blocked. An initial JavaScript syntax check on Node 12 rejected optional
chaining; the expression was simplified and the check passed afterward.

## Review and limits

The checkout already had an uncommitted mock specialist, documentation and check
script changes. Work continued on `cleanup/3d-printing-agent`; the specialist code
and its tests were retained and the documentation/check entry points were adapted.
No reset, stash, commit or push was performed. A copy of the starting tracked diff
was saved outside the repository in `/tmp/photonics-precleanup-user-changes.patch`.
No legacy archive was added to the repository.

The core `printer_client.py`, `printer_commands.py`, `printer_policy.py`,
`printer_watchdog.py`, `fastmcp_server.py` and operator-editable `simple.py` were
verified byte-for-byte unchanged from the starting tracked versions. Terminal
selection now defaults to mock and needs explicit `--real` for SSH; its physical
confirmation, mesh policy and motion safeguards remain covered by retained tests.

Removed tests belonged to perception/erasing workflows or their former UI.
Generic remote-access tests were adapted, and new dashboard/default-mode tests
check the changed boundaries. The old environment export and inactive nested CI
workflow were replaced with direct test dependencies and a root CI workflow.
CI itself was not run on GitHub; this validation is the local Linux run.

The dashboard was exercised over HTTP; no interactive browser rendering or
cross-browser layout suite was run. Python 3.8 checks establish grammar
compatibility, not execution on a printer. No physical device was contacted or
operated; no homing, axis movement, heating, extrusion, mesh or firmware change
occurred. Mock results do not establish physical clearance, print quality or a
successful print. OpenSCAD generation/export, previews, slicing, RAG, chatbot
orchestration and print-job control remain future work.
# Approved print-job integration — 2026-10-29

Added the separate opt-in dashboard print-job service. Validation for this change:

- `python3 scripts/check.py` passed the complete suites and dependency checks.
  After final changes, the affected specialist suite passed 69 tests (3 opt-in
  checks skipped), and dashboard suite passed 28. The unchanged controller suite
  passed all 144 tests in the complete run. Local test sockets needed execution
  outside the outer sandbox; all printer transports remained fixtures/mocks.
- `RUN_SLICER_INTEGRATION=1 ... unittest ... test_print_jobs.py` passed 15 tests,
  including actual isolated OpenSCAD and PrusaSlicer for both PLA and PETG profiles.
- `scripts/check_model_preview.py --print-fixture` passed in real Chrome:
  real compiled/approved STL, real isolated slicing, visible toolpath, separate
  job/setup confirmations, one test-only start and matching progress. Existing
  preview, dimensions, rotation, zoom, downloads, revision reset and failure
  display checks also passed. A visibility issue found during image review was
  corrected and verified in the final browser run.
- New remote helper syntax parses under Python 3.8; documentation local links
  and `git diff --check` pass.

Separate operator-authorized hardware observation: shared SSH status/readiness
queries succeeded on the Ender-3 V3 KE. Firmware reported standby, 0.4 mm nozzle,
1.75 mm filament, saved default bed mesh and zero heater targets. This is not
physical nozzle/material verification. No file was uploaded and no print,
homing, heating or movement was started by this implementation session. The
operator has not yet identified the loaded filament. Profiles and print quality
have not been physically qualified. Live AI generation was not tested; the newly
started dashboard process has no Groq key.

## Creality Print migration — 2026-10-29

The production slicer is now Creality Print, with no automatic PrusaSlicer fallback.
The official Linux 7.3.0.6157 AppImage matched its published SHA-256 before extraction.
On this Ubuntu 22.04 workstation it needed privately extracted newer loader/C++/
GLib/HarfBuzz/TIFF libraries plus parallel Soup3/WebKit4.1 libraries. Packages came
through signed Ubuntu APT metadata; no system packages or sources were changed.
The compatibility bundle and its resource alias remain inside the slicer sandbox.
Actual isolated CLI help confirmed `--cli`, `--slice`, resolved JSON preset flags
and `--need-gcode-file`.

- The complete hardware-free check script passed (144 controller tests and 28
  dashboard tests, including dependency checks). After the final adapter and
  migration tests, the affected specialist suite passed 78 tests with 3 opt-in
  checks skipped. The dashboard suite passed 28 tests again after installation.
- The opt-in real compiler/Creality slicing suite passed all 16 tests, including
  both PLA and PETG. A 20 × 10 × 5 mm asymmetric model verified bed centering,
  retained XY orientation, nonempty bounded toolpaths and real time/filament estimates.
- The real Chrome print-fixture workflow passed with actual OpenSCAD and Creality
  Print: model review/approval, local slicing, visible path, separate setup/job
  confirmations, a single fixture-only start and matching progress. No live AI
  requests or physical printer connections occurred in those tests.
- Preset inheritance/provenance, runtime/profile changes, corrupt/unverified
  archives, legacy job invalidation, missing output despite zero exit, bounded
  arguments/environment and absence of Prusa fallback have offline tests.

The operator clarified that the loaded filament is PETG. This is operator-provided
material information, not camera verification. No real job was uploaded, started,
heated or moved during the migration. The 0.4 mm nozzle remains based on previous
firmware information pending the operator's setup check. Actual print quality is
still unqualified. The restarted dashboard has the real SSH adapter explicitly
configured but makes no hardware request on startup. AI generation still needs a
Groq key in that backend; saved/manual models remain usable.

## Return home after print completion — 2026-10-29

Newly sliced PLA/PETG files switch heaters and the fan off, lift Z 5 mm, return
to the already-homed XY origin at 30 mm/s, then disable motors. Z stays raised;
no extra firmware homing or post-completion network command is introduced. The
job review displays the finish behavior. Resolved settings include the new end
G-code, so previously prepared files require fresh preparation and approval.

The validator requires the exact closing sequence and checks its moves against
the existing limits. Tests cover a missing/altered finish, Z homing/descent,
reordered moves, commands after the finish, repeated homing during the job and
the 240 mm model / 245 mm lifted-Z boundary. The real isolated OpenSCAD/Creality
suite passed all 19 tests, including PLA and PETG output. Extrusion-path centering
and orientation remain correct; total travel bounds now also include XY home.
The complete hardware-free checks passed: 81 specialist tests (3 opt-in skips),
144 controller tests, 28 dashboard tests and both dependency checks. Namespace
integration and mock socket tests ran outside the restrictive execution sandbox.
No physical printer connection or motion was used for this change. Restart the
dashboard backend and prepare a new job to apply it; existing uploaded or active
files are unchanged.

## Standalone printer-agent project — 2026-10-29

The project uses the display name PhotonicsAI Printer Agent and repository name
`PhotonicsAI-Printer-Agent`. Dashboard, manifest, schema titles, examples and
documentation use that name. The existing `3d-printing` delegation identifier
and package/controller paths remain compatible. Original source attribution and
the retained controller/viewer licenses are preserved.

The checkout was renamed locally with a compatibility symlink for existing
terminals and virtual environments. From the new directory, all 81 specialist
tests (3 opt-in skips), 144 controller tests, 28 dashboard tests and dependency
checks passed. All 19 opt-in local slicing tests passed, including both materials.
JSON files and local documentation links validated. The candidate working tree
contained no detected provider credentials, private keys or printer address;
runtime environments, configuration, generated jobs and slicer downloads stay
ignored. No physical printer connection was used for this publication work.
