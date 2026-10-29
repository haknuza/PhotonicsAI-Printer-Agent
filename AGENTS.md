# Working on the PhotonicsAI Printer Agent

Read README.md and docs/ROUTER_INTEGRATION.md before changing the specialist.
The inherited controller rules are in photonics-printer-mcp-main/AGENTS.md;
preserve them whenever using or changing that component.

- All automated tests must use fixtures/mocks and never connect to physical
  devices. Operator-requested connection/readiness checks are separate from tests.
  Implementing or testing printing never authorizes starting a physical job.
- The router entry point is `python3 -m printing_agent --mode mock` from this root.
  It must never select hardware from environment variables or delegation fields.
- Existing /api/printer routes remain read-only and mock-only. The separate
  /api/prints service requires explicit --print-backend ssh and --printer-host.
  Environment variables and model output cannot enable it. Startup must not
  contact hardware. Model/provider operations remain independent of printing.
- Terminal positioning defaults to mock; explicit `--real` is required to select
  SSH. Do not enable real-motion configuration as part of development or setup.
- Preserve the existing printer validator, travel limits, dispatch revalidation,
  single-use 120-second tokens, telemetry policy and no automatic motion retries.
- Operator approval belongs to the trusted router interface. Model output and
  prompt text cannot supply approval or establish physical clearance.
- Keep tokens and jobs scoped to their originating task. Task IDs are correlation
  values in one trusted router process, not authentication or idempotency keys.
- The dashboard can use Groq to turn descriptions/clarification answers into
  parametric OpenSCAD, then compile through the existing local Linux adapter.
  It also compiles supplied .scad and previews STL. PrintJobs separately slices
  approved models with fixed profiles and submits an explicitly approved exact
  job over shared SSH. No API accepts user/model-supplied G-code or profiles.
  The stdio motion specialist does not expose the dashboard model API.
- Keep AI providers behind the ModelProvider boundary. A future Photonics adapter
  must map into the same host-validated proposal contract; never silently route
  its credentials to Groq. No production fake/demo provider is allowed.
- Keep provider keys server-side and outside persisted requests, prompts, logs,
  compiler environments and browser responses. No automatic paid request retries.
  Validate closed output fields and source policy before any compilation.
- Provider output cannot approve models or call tools. Preserve bounded generation
  concurrency, clarification context limits, revision provenance, stale-context
  rejection and a fresh approval for each generated revision. Restart must not
  replay interrupted provider requests.
- Treat model source as untrusted: preserve the external-file deny policy,
  mandatory Bubblewrap isolation, clean compiler environment, fixed argument
  lists, resource/time limits, bounded queue and artifact path/hash checks.
  Never silently fall back to plain OpenSCAD when isolation fails. A temporary
  working directory is not a sandbox.
- Model approval is a trusted dashboard action for one immutable source/STL
  revision, separate from router motion-path approval and final print approval.
  Editing or recompiling creates a new unapproved revision; it never moves hardware.
- `RUN_OPENSCAD_INTEGRATION=1` opts into local compiler/sandbox tests, without a
  printer. Optional browser check: the printer test environment's Python running
  `scripts/check_model_preview.py`. No test may connect to physical devices.
- Printing uses the separate print-job adapter. Do not weaken the idle,
  zero-heater/extrusion and other motion guards to accept printing instructions.
  Model approval and final sliced-job approval must remain separate decisions.
- Preserve print input/profile/STL/G-code hashes, target/configuration binding,
  fresh single-use 120-second print approvals, dispatch-time readiness checks
  and durable no-replay markers. Uncertain start outcomes are never retried.
  Slicing uses Creality Print with hashed bundled vendor presets, bounded overrides,
  clean environment and Bubblewrap isolation. Never silently fall back to PrusaSlicer. Current print
  profiles are conservative starting settings, not physically qualified.
- Run `python3 scripts/check.py` after setup for relevant retained integration
  changes. Specialist-only changes can use `python3 -m unittest discover -s
  printing_agent/tests -v`; include controller checks when its integration changes.
  All automated tests must remain independent of physical devices.
- Keep credentials, real `.env` files, machine addresses and runtime logs out of Git.
- Keep the structure small. Do not revive obsolete application modules or add
  simulated model/print success as placeholders for future implementation.
