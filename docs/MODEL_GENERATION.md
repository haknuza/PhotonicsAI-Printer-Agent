# Description-to-model generation with Groq

The dashboard now accepts an object description, asks clarification questions
when needed, generates parametric OpenSCAD and submits that source to the existing
isolated Linux compiler. The resulting source, STL, dimensions, preview, revisions
and explicit model approval use the same validated model pipeline as manual code.
No printer connection, slicing, heating, extrusion or print submission is involved.

## Local setup

Use the existing Linux/OpenSCAD setup in [OPENSCAD.md](OPENSCAD.md). No additional
Python SDK dependency is needed: the Groq adapter uses Python's HTTPS client and
the official Chat Completions endpoint. Existing manual compilation remains
available even if the AI provider is missing, unavailable or out of quota.

From the repository root in Bash:

```sh
python3 scripts/setup.py
export MODEL_PROVIDER=groq
export GROQ_MODEL=openai/gpt-oss-120b
export OPENSCAD_BIN=/usr/bin/openscad
lab-agents/.venv/bin/python lab-agents/app.py --prompt-groq-key --printer-mode mock --port 8770
```

Paste the actual key at the hidden prompt and press Enter. `--prompt-groq-key`
reads it directly into the backend process, with no file save or API call. Empty
input, cancellation or an unavailable hidden terminal stops startup. An exported
`GROQ_API_KEY` still works when the prompt flag is omitted.

The hidden prompt avoids placing the real key in a command or committed file.
Do not paste it into the model description or send it from the browser. Settings
are read at backend startup, so restart after changing credentials/model settings.
`.env` is not auto-loaded; the ignored-file example documents names but contains
no working key. Keep keys out of source control. `GROQ_MODEL` can select another
Groq Chat Completions model supporting JSON-object responses; the default is
`openai/gpt-oss-120b`. Model availability/permissions/quota depend on your account.

Stop an earlier dashboard before restarting. If the selected port is already in
use, startup fails before asking for a key, so an old page is not mistaken for a
new backend. A locked model store also produces a clear restart message. Wait
for `AI configuration: ready` and `Running on…` in the terminal and open the printed
URL. Compare the terminal Backend PID to the page Backend PID. A ready local
configuration is not proof of Groq authentication; only a real request verifies it.

Open <http://127.0.0.1:8770>. The AI status shows the configured provider/model or
a clear configuration error. The model editor and hand-written example still work
without a key. No background request is sent just by opening the page.

## Usage

1. Describe the object with units, important dimensions, holes and fit requirements.
   For example: “Make a spacer with a 24 mm outside diameter, an 8 mm through-hole
   and a 6 mm height.” The prompt asks the AI to use editable named parameters.
2. Click **Generate model**. If essential information is missing, answer the shown
   design questions and click **Send answer & generate**. Clarification sends no
   source to the compiler and cannot approve a model.
3. The backend validates the generated proposal/source and automatically passes
   accepted code to real OpenSCAD. The UI shows generating, compiling, then the
   actual preview or failure. Code is visible in the editor and downloadable.
   Review the explanation, assumptions, code and dimensions; AI output can still
   be geometrically wrong even if it compiles successfully.
4. Ask for a change, such as “Make the height 9 mm and keep the other dimensions.”
   The selected source (including manual editor changes) becomes revision context.
   Compiler diagnostics from a failed parent job are included for explicit repair
   requests. There is no automatic paid retry or automatic repair loop. If validated source
   is generated but compiler submission fails (for example, queue saturation or
   stale revision context), its saved request preserves the uncompiled draft.
   The editor can recover it from request history; it has no STL or approval.
5. Each successful generation creates its own source/STL revision with provider,
   model, request ID, prompt, assumptions and source hashes in provenance. Approval
   never carries to a new revision. If another edit finishes during generation,
   the stale AI result is rejected instead of replacing the newer model.
6. Select a saved AI design request to resume a pending clarification or inspect
   its outcome after reloading. Select saved model revisions for their source,
   preview and downloads. Use **New model** to begin an independent design.

The initial system prompt asks for essential missing dimensions rather than silent
invention. Noncritical/default dimensions must be listed as assumptions. This is
model behavior requested by the prompt, not a guarantee of semantic correctness.
The host independently enforces the response contract, source/file policy,
compilation boundary, artifacts and approval rules. It cannot certify fit,
load-bearing performance, printability or conformity to the description.

## Provider boundary and future Photonics API

`printing_agent/providers.py` defines the `ModelProvider` protocol:

```python
name: str
model: str
generate(turns: list, base_source: str) -> dict
```

A provider returns one untrusted design proposal with exactly:

```json
{"outcome":"ready","title":"Parametric spacer","source":"... complete .scad ...",
 "explanation":"...","assumptions":[],"questions":[]}
```

Outcomes are `ready`, `needs_clarification` and `unsupported`. Clarification must
have empty source and one to three questions. A refusal has no source/questions.
Ready source must pass the same source policy as manual input before compilation;
unknown fields, including approvals and tool calls, are rejected. Code fences,
partial files or commands are not transformed into shell commands or executed.

The implemented Groq adapter sends JSON-object mode requests with a fixed system
contract and host validation. It makes one Chat Completions request, with a
45-second network timeout, an 8192 completion-token budget and a 256 KiB response
limit. It rejects truncation, duplicate JSON keys, nonfinite JSON, tool invocations,
malformed responses and unrecognized design fields. It does not follow redirects
with the bearer key, return raw provider error bodies or retry requests.
Authentication, connection, timeout, model/request and rate/quota errors have clear
codes. A numeric Retry-After value is exposed only as bounded seconds; it never
schedules a retry. There is no Groq SDK with hidden retry behavior.

To add Photonics later, implement that protocol and register its own factory path
in `create_provider()` using the real Photonics API/authentication contract. The
UI, generation request lifecycle, OpenSCAD adapter and model approval can remain
unchanged. No Photonics adapter or endpoint is invented now: setting
`MODEL_PROVIDER=photonics` currently reports unsupported configuration and never
falls back to Groq or sends a Photonics key there.

The provider has no access to printer tools, subprocess execution or model approval.
Only the host can call the compiler after validation. The stdio motion specialist
is separate; its protocol still exposes no AI generation or physical printing.

## HTTP operations

These routes use the existing model API's authentication, same-origin POST checks
and `X-Lab-Control: local` header. JSON bodies have closed fields; clients cannot
supply API keys, provider names, arbitrary conversation roles, job IDs or approvals.

| Operation | Request |
| --- | --- |
| Provider availability/model | `GET /api/models/config` → `generation` |
| Submit description | `POST /api/models/generate` with `{"description":"..."}` |
| Request a revision | Same POST plus `"parent_job_id":"MODEL_JOB_ID"` and optional `"base_source":"... current editor source ..."` |
| Answer clarification | Same POST with `"description":"... answer ...","previous_request_id":"GENERATION_REQUEST_ID"` |
| Poll | `GET /api/models/generations/GENERATION_REQUEST_ID` |
| Saved request history | `GET /api/models/generations` |

Submission returns 202 and a server-owned generation request ID. Poll through
`queued`, `generating`, `compiling` until `ready`, `needs_clarification`,
`unsupported` or `error`. When a compile job exists, `job` contains the existing
model response with artifact URLs, exact hashes and actual compiler diagnostics.
Fetch source/STL and approve through the existing model API. Request text/JSON
content is displayed as text, not injected into HTML.

Clarification replies reuse server-stored conversation/source/parent context;
they cannot replace it with client history. A clarification is consumed only once.
Descriptions are at most 4000 UTF-8 bytes, design conversations at most six user
turns, and base/generated source at most 128 KiB. There are two generation workers,
at most four pending/running provider calls and at most 100 saved requests. The
compiler's separate queue and limits remain unchanged. Saturation fails clearly;
there is no hidden work queue or unbounded conversation growth.

## Access-denied troubleshooting

The client sends `User-Agent: PhotonicsAI-3D-Agent/1.0`. A local unauthenticated
model-list diagnostic reproduced HTTP 403/error 1010 with Python's default client
header, while the application's descriptive header reached the API and received
the expected HTTP 401/invalid-key response (no key was sent). This verifies the
transport fix, not a user's API key or permission to generate with a given model.
The previous blanket `PROVIDER_AUTH` classification of all HTTP 403 responses was
misleading and has been replaced with bounded, redacted classification:

- `PROVIDER_GATEWAY_BLOCKED`: recognized gateway error 1010, before API
  authentication. Restart the updated backend; if it persists, inspect the
  network/proxy policy or contact Groq support. Do not infer an account problem.
- `PROVIDER_MODEL_PERMISSION`: Groq's recognized organization/project model-block
  code. Check the selected model in Organization → Limits or Projects → Limits.
  An organization block cannot be expanded by project settings; use an explicitly
  permitted model or have the appropriate administrator change permissions.
- `PROVIDER_ACCESS_DENIED`: another HTTP 403. Check account/model permissions and
  network/proxy policy; successful authentication is not established by this code.
- `PROVIDER_AUTH`: HTTP 401, for rejected/missing authentication credentials.

The code inspects at most 8 KiB of a 403 response and only returns fixed messages
for allowlisted codes/markers. Raw provider bodies, supplied messages and keys
are never exposed or saved. There is no automatic generation retry or fallback
to another model. After restarting, submit the description again explicitly;
old saved failures remain historical records.

See [Cloudflare client-header guidance](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/),
[Groq model permissions](https://console.groq.com/docs/model-permissions) and
[API error codes](https://console.groq.com/docs/errors).

## Data and execution boundary

Descriptions, relevant design conversation, selected source and bounded parent
compiler diagnostics are sent to the configured cloud provider when you click
Generate/Send answer. These are stored locally in the existing private ignored
model job store under `generations/`; provenance also lives in the model revision.
API keys are not part of those records, prompts, browser responses, compiler
arguments or the compiler environment. Do not put secrets in descriptions/source:
those are the design inputs intentionally sent to the provider.

All source remains untrusted, regardless of provider. The empty external-asset
allowlist, mandatory Bubblewrap isolation, source read-only mount, clean environment,
resource/time limits, fixed CLI arguments and validated artifact paths/hashes are
preserved. A temporary directory and prompt instructions are not sandboxes. See
[the complete execution boundary and limits](OPENSCAD.md#untrusted-source-execution-boundary).
This is one trusted local backend/LAN account, not per-user authenticated public
hosting. Job/request IDs are correlation values, not authorization credentials.

Restart marks interrupted generation as an error and never replays cloud calls.
Already submitted compiler jobs follow their existing restart policy. Completed
requests/revisions remain available, with no automatic deletion or approval reuse.
Archive saved jobs/requests only with the backend stopped. Provider behavior,
cloud data retention, billing and credentials are outside the compiler sandbox;
review your provider/account settings for deployment.

## Verification and its limits

```sh
python3 scripts/check.py
RUN_OPENSCAD_INTEGRATION=1 python3 scripts/check.py
photonics-printer-mcp-main/.venv/bin/python scripts/check_model_preview.py --generation-fixture
```

Unit tests mock the Groq HTTP transport and use explicitly scripted provider
fixtures. The browser option starts a **test-only fixture server**, compiles its
source using real isolated OpenSCAD and checks questions, preview, dimensions,
model approval, revision reset and unsafe-code rejection. It is not a production
provider/demo mode and is not evidence that Groq generated that fixture source.
The test strips cloud keys and makes no paid request. The regular browser check
verifies missing-key guidance and that AI generation is disabled while manual
source compilation remains usable. Without the flag, the browser
check uses the actual application with generation disabled and manual compilation.

No live Groq request has been verified in this workspace because no API key was
configured. To test real model quality after configuration, submit a simple
well-dimensioned description in the normal dashboard and inspect the resulting
source/preview. Do not infer success from the fixture tests alone.

Implementation references: [Groq Chat Completions API](https://console.groq.com/docs/api-reference),
[JSON-object response mode](https://console.groq.com/docs/structured-outputs#json-object-mode),
and [supported models](https://console.groq.com/docs/models). See
[validation](VALIDATION.md) for checks actually run.
