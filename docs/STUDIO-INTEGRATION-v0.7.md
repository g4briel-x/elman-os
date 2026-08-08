# ELMAN-OS v0.7 — Studio Integration

## Purpose

This increment implements roadmap milestone 6: a read-only ELMAN Studio view
over the integrity-bound orchestration contracts introduced during v0.7.

Studio can now expose, from one final-verification request and its optional
signed report:

- the execution plan and ordered steps;
- selected agents and capabilities;
- deterministic step progress;
- explicit human approvals;
- project-memory decisions and revision provenance;
- output, artifact and external evidence;
- policy findings, execution errors and blocked steps;
- metacognitive decisions and findings;
- all nine final-verification gates;
- the final report trust state.

The integration does not grant new execution authority. It is a projection and
inspection boundary, not an orchestrator.

## Components

`elman_os.studio_v07` provides:

| Component | Responsibility |
|---|---|
| `StudioV07Projector` | validates request/report bindings and creates one deterministic snapshot |
| `StudioDashboardSnapshot` | immutable, SHA-256-bound Studio read model |
| `StudioStepCard` | step, agent, capability, state, progress and latest event |
| `StudioAgentCard` | groups selected agents, capabilities and assigned steps |
| `StudioApprovalCard` | exposes required or granted plan, step and supervision approvals |
| `StudioMemoryCard` | exposes latest memory revision, provenance and decision/result coherence |
| `StudioEvidenceCard` | exposes evidence metadata and source hashes without payloads |
| `StudioIssueCard` | normalizes policy findings, execution errors, supervision findings and plan blocks |
| `StudioSupervisionCard` | exposes metacognitive action, confidence, risk, findings and rationale |
| `StudioGateCard` | exposes each final-verification gate and deterministic issue codes |
| `load_dashboard_snapshot` | reads explicit local request/report JSON files |
| `launch_studio_v07` | launches the optional read-only Flet dashboard |

## Trust states

Studio uses four final states:

| State | Meaning | Completion authorized |
|---|---|---|
| `not-run` | no final report was supplied | no |
| `signature-unverified` | a report exists but no trusted signer was supplied | no |
| `rejected` | the report signature is valid but at least one mandatory gate failed | no |
| `verified` | the signature is valid and all nine gates passed | only if every displayed approval is granted |

The report status alone is never sufficient. A serialized report claiming
`verified` remains `signature-unverified` until its HMAC is checked with a
caller-supplied `FinalReportSigner`.

## Source binding

Before projection, Studio verifies:

1. the final-verification request hash;
2. the final report hash, when supplied;
3. equality of the report and request verification identifiers;
4. equality of their request hashes;
5. equality of plan and project identifiers;
6. equality of plan-state hashes;
7. equality of journal hashes;
8. the HMAC report signature, when a signer is supplied.

A mismatch raises `StudioV07IntegrityError`. Studio does not attempt a partial
display that could combine evidence from one execution with a report from
another.

## Dashboard snapshot

`StudioDashboardSnapshot` is immutable and canonical. Its SHA-256 digest covers
all displayed security-relevant fields:

- request, plan, project and journal identity;
- steps, agents and progress;
- approvals and their references;
- memory revision metadata and decision links;
- evidence references and hashes;
- issues and resolution references;
- metacognitive decisions;
- final gates and issue codes;
- report hash, key identity and signature-verification state.

Derived fields such as aggregate progress and completion authorization are
recomputed during deserialization. Supplying inconsistent derived values is an
integrity failure.

The snapshot intentionally excludes:

- HMAC secret bytes;
- prompts or model responses;
- artifact payload bytes;
- project-memory content beyond the validated title;
- environment variables;
- credentials and bearer tokens.

## Plan and progress

Steps retain the plan's deterministic topological order. Progress is derived
from the validated step state and is not accepted from UI input:

| Step state | Displayed progress |
|---|---:|
| `pending` | 0% |
| `approved` | 10% |
| `running` | 50% |
| `blocked` | 50% |
| `failed` | 50% |
| `completed` | 100% |

The latest hash-chain event timestamp associated with each step is displayed
when available. Aggregate progress is the arithmetic mean across plan steps.
It is informational and cannot advance execution state.

## Agents

Assigned steps are grouped by stable agent identifier. Each agent card exposes:

- assigned step identifiers;
- declared capability identifiers;
- active-step count;
- failed-or-blocked-step count.

Unassigned steps remain visible in the plan but do not fabricate an agent
identity.

## Explicit approvals

Studio exposes approval requirements at three boundaries:

- plan approval;
- step approval;
- metacognitive supervision approval.

An approval is `granted` only when the validated source contract contains a
portable approval reference. Studio never generates a reference and never
turns a checkbox into an approval record.

The v0.7 dashboard is read-only. Creating, revoking and persisting approvals
remains the responsibility of the orchestration boundary and its append-only
journal.

## Project memory

Memory revisions are grouped by `memory_id`, and the highest validated revision
is displayed with the total revision count. The card includes:

- memory kind and state;
- payload availability after retention;
- payload and revision hashes;
- origin type and source identifier;
- decision/result link state;
- linked evidence identifiers.

Decision link states are:

- `coherent`: expected and observed result hashes match;
- `incoherent`: hashes differ;
- `missing`: an active decision has no displayed outcome link;
- `not-applicable`: the memory item is not a decision.

Purged payloads remain auditable through their hashes and provenance; Studio
does not reconstruct or expose deleted content.

## Evidence

The request already combines automatic output-validation and artifact-payload
evidence with additional explicit evidence. Studio displays only:

- stable evidence identity;
- evidence kind and status;
- optional step identity;
- source reference and SHA-256 source hash;
- capture time.

Evidence payloads are not read by this module. A `verified` label reflects the
validated evidence contract; external evidence validation remains the final
verifier's responsibility.

## Errors and blockages

Studio normalizes four issue sources:

1. final policy findings;
2. execution errors;
3. metacognitive supervision findings;
4. blocked or failed plan steps without a more specific step issue.

Resolution is displayed only when the source contract supplies a verified
resolution reference. A bare UI action cannot mark an issue resolved.

## Metacognitive reports

Each supervision card exposes:

- decision identity;
- `continue`, `correct`, `pause`, `stop` or `escalate` action;
- confidence in basis points;
- highest finding risk;
- finding count;
- approval requirement and reference;
- deciding agent and decision time;
- validated rationale.

Supervision findings also appear in the issue list so that corrective or
blocking conditions are not hidden behind an aggregate decision.

## Final verification

When a report is present, Studio requires exactly the nine v0.7 gates:

- plan completion;
- journal integrity;
- output validation;
- artifact integrity;
- evidence completeness;
- policy compliance;
- error resolution;
- decision coherence;
- supervision clearance.

Each gate displays pass/fail, checked count, issue codes and references. A
report with a missing or duplicated gate cannot become a valid dashboard
snapshot.

## Programmatic use

```python
from elman_os.final_verification import (
    FinalReportSigner,
    FinalVerificationReport,
    FinalVerificationRequest,
)
from elman_os.studio_v07 import StudioV07Projector

request = FinalVerificationRequest.from_json(request_json)
report = FinalVerificationReport.from_json(report_json)
signer = FinalReportSigner(
    key_id="key:final-verification-v1",
    secret=protected_key_bytes,
)

snapshot = StudioV07Projector(request, report, signer).project()
snapshot.verify_hash()

if not snapshot.completion_authorized:
    raise RuntimeError("Studio refuses final completion")
```

## Local dashboard launch

Install the optional Studio dependency, then launch the module directly:

```powershell
python -m pip install -e ".[studio]"

python -m elman_os.studio_v07 `
    --request .\.elman\final-request.json `
    --report .\.elman\final-report.json `
    --key-file .\.elman\secrets\final-report.key `
    --key-id "key:final-verification-v1"
```

The key is read only from the explicit file path. Raw secret text must not be
placed on the command line. The key bytes are never included in a dashboard
snapshot or object representation.

Launching with a report but without `--key-file` is allowed for inspection, but
the screen remains `signature-unverified` and completion is refused.

## Failure behaviour

The integration fails closed when:

- request or report JSON is malformed;
- a request, report, evidence or memory hash is invalid;
- the report references another request, plan, project or journal;
- the supplied signer cannot verify the report;
- the snapshot is modified after projection;
- a final report omits a mandatory gate;
- completion is claimed without a verified report;
- a required approval lacks a reference;
- a secret key is shorter than the final-verification minimum.

## Validation

The dedicated offline suite covers:

- plan, agent, progress, approval, memory, evidence, issue and supervision views;
- all nine final gates;
- successful and rejected signed reports;
- absent and unverified reports;
- wrong keys and cross-request report substitution;
- deterministic serialization and immutable snapshots;
- snapshot and derived-field tampering;
- local file loading without creation side effects;
- CLI key-file safety invariants.

## Current boundaries

This increment does not:

- execute or resume an orchestration plan;
- write or mutate approvals;
- edit project-memory records;
- resolve policy findings or execution errors;
- run generated code;
- deploy artifacts;
- fetch request, report or key material from the network;
- read secrets from environment variables;
- change the protected v0.6 Studio entry point or release checksums.

The v0.7 dashboard is launched through `python -m elman_os.studio_v07` during
development. Promoting it to the default `elman-os studio` entry point belongs
to milestone 7, when v0.7 release metadata and checksums are regenerated as one
coherent release operation.
