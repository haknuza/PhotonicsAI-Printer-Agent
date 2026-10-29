# General-purpose printer agent: intended architecture

This is the intended product scope and implementation plan. Camera inspection,
tool-mode selection and laser operation remain planned. The dashboard now has a
separate [approved print-job adapter](PRINTING.md); physical print quality has
not been validated. See [router integration](ROUTER_INTEGRATION.md) for the current
mock motion stdio contract.

## Goal and integration ownership

Build one conversational agent for the printer as a configurable machine.
Regular 3D printing is its first tool mode. Later, a laser or another attachment
can add a mode with its own setup checks, planning and execution adapter.
The agent should help with different printer needs: explain capabilities,
check readiness, guide setup, create or revise a model, prepare a job and
eventually execute and monitor an approved job.

The project is intended to be pushed as a working standalone component. The
Photonics chatbot team will connect it as a delegated agent. Keep its entry
points, capability descriptions, structured results and local validation
documented so that integration does not depend on dashboard internals.
The actual upstream integration contract still needs agreement with that team;
the current local stdio contract is a starting point.

```text
User ↔ Main Photonics chatbot
                 ↕ delegation and results
        Printer agent
          ├─ Conversation/task context and capability discovery
          ├─ Tool-mode registry and readiness checks
          ├─ Camera observations + controller state + operator setup confirmation
          └─ Mode adapters
               ├─ 3D printing: design → model review → slicing → job review → execution
               └─ Future attachments: their own setup, planning and execution
```

The agent owns printer-specific task reasoning and asks for missing information
through the chatbot. A model can propose a plan; host code validates requests,
checks readiness and enforces execution permissions. The existing replaceable
model-generation provider remains separate from the upstream chatbot connection.

## Tool modes and readiness

Keep two concepts separate:

- **Tool mode:** what the machine is configured to do, such as `3d_printing`
  or a future `laser` mode.
- **Execution backend:** where operations run, such as `mock` or a future
  explicitly configured hardware adapter. The existing `--mode mock` and
  manifest `modes` field describe this backend, not installed attachments.

A request to use a tool mode does not establish that the corresponding attachment
is installed. A future registry should describe each mode's supported operations,
required attachment, setup checklist, available adapters and approval requirements.
Discovery must distinguish software support from the current physical setup.
An unimplemented laser adapter remains unsupported even if a camera sees a laser.

Proposed readiness outcomes, separate from existing v1 response statuses:

| Outcome | Meaning | Agent response |
| --- | --- | --- |
| `unsupported` | Required mode or execution adapter is not implemented/configured | Explain the missing capability; do not create a runnable job |
| `needs_setup` | A known attachment or setup requirement is unmet | Give the operator concrete setup steps and wait |
| `unknown` | Evidence is missing, stale, obstructed or conflicting | Request another observation or the necessary operator check |
| `blocked` | A controller fault, active job or required check prevents operation | Report the blocking condition and wait for resolution |
| `ready` | Required setup checks are satisfied for the proposed operation | Continue to planning/review; readiness does not authorize execution |

The agent can discuss a mode or prepare a design before its physical setup is
ready. Physical execution requires a fresh readiness check for the selected mode
and operation. Sharing one machine also requires one execution owner: concurrent
chatbot tasks must not switch tools or start conflicting jobs.

## Camera-assisted setup

The camera should help determine whether the chosen mode is available now or
requires a human to configure the printer. Relevant observations may include
the visible attachment, build surface, workpiece and obstructions, within the
limits of the actual camera view and a validated inspection implementation.

Combine observations with controller state and mode-specific operator checks.
A camera cannot establish hidden wiring, firmware configuration or every required
interlock. Each readiness result should identify its evidence, capture time,
uncertainties and unmet requirements. Missing or ambiguous evidence stays unknown;
it must not be translated into a confident ready result.

For a setup change, the agent explains what a human must change, waits for the
operator to report completion, obtains fresh observations and rechecks readiness.
It must not infer that hardware changed because the user selected a mode in chat.
Recheck after a tool change, relevant controller change or stale observation;
any approval dependent on the changed setup must be renewed. Camera inspection
never substitutes for job approval or a mode's required hardware protections.

Example future interaction:

1. User asks the Photonics chatbot to print an object.
2. The chatbot delegates the printer task; the printer agent clarifies the design
   and selects `3d_printing` as the requested tool mode.
3. If the mode's adapter exists, the agent checks setup using current observations
   and controller data. If a different attachment is fitted, it asks the operator
   to install the required printing tool and complete that mode's checklist.
4. After the operator completes setup, the agent rechecks. Uncertain checks remain
   unresolved and are shown to the user.
5. The user reviews the model, requests revisions and approves a specific design.
6. A separate slicer/print-job adapter prepares the exact job for final approval.
7. The agent revalidates readiness and approval before submission, then reports
   actual progress and failures through the chatbot.

These steps describe the target multi-mode workflow. The current dashboard has
model approval, separate final job approval, slicing and optional SSH submission;
it has no tool-mode registry or camera inspection.

## Extending the machine

Add new attachments through mode adapters. Each adapter needs explicit supported
operations, setup/readiness requirements, planning/output formats, execution
validation, approval rules and stop/fault behavior. A laser mode will require its
own implementation and hardware-specific checks before it can be advertised as
operational. Do not send its jobs through the current positioning validator or
invent generic execution success for an unfinished adapter.

Keep model approval, positioning-path approval and final mode-specific job approval
distinct. The present controller's idle and zero-heater/extrusion guards remain
intact; printing uses the separate print-job adapter.

## Current foundation and delivery milestones

| Area | Current repository | Remaining work |
| --- | --- | --- |
| Chatbot delegation | Documented mock-only stdio specialist | Agree upstream contract and connect the general task workflow |
| Design | Provider boundary, Groq generation, local OpenSCAD, revision preview and model approval | Live provider validation and upstream exposure |
| Printer operations | Guarded mock positioning plus a separate opt-in SSH print-job adapter/progress reads | Physical print validation and additional mode adapters |
| Tool modes | No tool-mode registry | Registry, mode adapters and readiness lifecycle |
| Camera | No active camera/inspection service | Acquisition, mode-specific inspection and evidence freshness |
| Human setup | No attachment setup workflow | Guided setup, trusted confirmation and reinspection |
| 3D print jobs | Isolated slicing, exact-job approval, SSH submission and explicit progress reads | Physically qualify settings and first supervised prints |
| Future attachments | Not implemented | Add and validate each mode independently |

Suggested sequence:

1. Deliver and document the existing standalone design/mock-motion functionality
   accurately, including what the chatbot team can call today.
2. Add a tool-mode registry and hardware-free readiness/setup workflow, with
   explicit missing-camera and unsupported-adapter outcomes.
3. Implement camera acquisition and validate attachment/setup checks against the
   actual printer. Retain human checks for requirements outside camera visibility.
4. Physically validate the first operational mode's profiles and approved job
   adapter, then integrate the agreed chatbot contract.
5. Add other attachments only when their hardware, adapter and checks exist.

For a handoff called a working general-purpose printer agent, demonstrate mode
discovery, honest readiness outcomes, setup guidance, task continuation and one
implemented end-to-end mode. A mock demonstration must be labeled simulated;
model generation alone is not evidence that physical printing works. Development
and automated checks in this repository remain mock-only under [AGENTS.md](../AGENTS.md).
