# ELMAN-OS v0.7 — Fail-Closed Final Verification

## Purpose

This increment implements roadmap milestone 5: a deterministic final verifier
that prevents incomplete, inconsistent, insufficiently evidenced or unsafe
executions from being marked as completed.

The boundary is local and offline. It does not call an AI provider, execute
generated code, mutate a plan, write project artifacts, read environment
variables or access the network.

## Mandatory verification gates

`FinalVerifier` evaluates exactly nine gates:

| Gate | Required condition |
|---|---|
| `plan-completion` | plan and every step are completed, agents are assigned and required approvals are referenced |
| `journal-integrity` | the hash chain is valid, every step has a completion event and the journal ends with `plan.completed` |
| `output-validation` | every step has exactly one accepted output-validation result with no rejected or review records |
| `artifact-integrity` | every accepted artifact has one verified byte-level payload result and counts remain consistent |
| `evidence-completeness` | every step meets the configured minimum of verified evidence and no evidence remains failed or under review |
| `policy-compliance` | every declared policy finding is resolved with verified resolution evidence |
| `error-resolution` | every declared execution error is resolved with verified resolution evidence |
| `decision-coherence` | every active project-memory decision has one evidence-backed link whose expected and observed result hashes match |
| `supervision-clearance` | a metacognitive decision bound to the final plan and journal explicitly authorizes `continue` |

All nine gates are mandatory. The policy constructor rejects attempts to
disable completion, journal, decision, supervision, signature or fail-closed
requirements.

## Bound input snapshot

`FinalVerificationRequest` embeds canonical, immutable snapshots of:

- the final execution plan and its SHA-256 state hash;
- the complete sealed execution journal;
- output-validation results;
- artifact-payload verification results;
- additional evidence records;
- policy findings and their resolution evidence;
- execution errors and their resolution evidence;
- decision-to-outcome links;
- metacognitive supervision decisions;
- project-memory revisions;
- the verifier identity and UTC request time.

The request validates each embedded contract before verification. It also
checks cross-boundary bindings:

- output results must reference a real prefix of the final journal;
- output and payload identities must match the final plan step and agent;
- payload results must reference an embedded output result;
- known evidence references must carry the exact embedded source hash;
- unknown evidence sources must use the explicit `external:` namespace;
- resolution and decision-link evidence must exist and be verified;
- memory records must belong to the plan project;
- decision links must reference the current decision revision;
- supervision decisions must belong to the same plan and project.

Malformed or tampered input raises an integrity error before a completion
decision can be produced. This is intentional fail-closed behaviour.

## Evidence model

`FinalEvidenceRecord` stores no evidence payload and no secret. It records only:

- a stable evidence identifier;
- a bounded evidence kind;
- `verified`, `failed` or `requires-review` status;
- plan and optional step identity;
- source reference and SHA-256 source hash;
- capture time;
- its own deterministic evidence hash.

Output-validation and artifact-payload results automatically produce evidence
records during request capture. Additional test, approval, policy, decision or
external evidence can be supplied explicitly.

An unresolved policy finding or execution error contains no resolution
reference. A resolved item must point to existing evidence with `verified`
status. This makes a bare `resolved=true` assertion insufficient.

## Decision and result coherence

Project-memory decisions are immutable and permanently retained by milestone
4. The final verifier reads the latest revision supplied for each memory item.

Every active decision requires exactly one `FinalDecisionOutcomeLink`. The link
binds:

- the decision memory identifier;
- the current memory revision hash;
- the expected result hash;
- the observed result hash;
- one or more verified evidence identifiers;
- the link timestamp and link hash.

Different expected and observed hashes reject final completion. Links to
inactive, unknown or stale decision revisions are also rejected.

## Signed final reports

Every accepted or rejected decision produces a `FinalVerificationReport`.
Reports include all gate outcomes, source snapshot hashes, verifier identity,
UTC timestamp, key identifier and signature algorithm.

`FinalReportSigner` uses HMAC-SHA-256 and requires at least 32 bytes of key
material. The key:

- is supplied directly by the caller;
- is never read from an environment variable by this module;
- is excluded from object representations;
- is never serialized into the request or report;
- is required again to verify the report signature.

A report is signed even when its status is `rejected`. This preserves an
auditable denial and prevents an unsigned rejection from being rewritten as a
verified completion.

HMAC provides integrity and authenticity to parties that share the key. It is
not a public-key signature and does not provide non-repudiation between key
holders. Key distribution, rotation and protected storage remain deployment
responsibilities.

## Minimal use

```python
from elman_os.final_verification import (
    FinalReportSigner,
    FinalVerificationPolicy,
    FinalVerificationRequest,
    FinalVerifier,
)

policy = FinalVerificationPolicy(
    policy_id="policy:final-verification-v1",
)

request = FinalVerificationRequest.capture(
    verification_id="final-verification:execution-001",
    policy=policy,
    plan=completed_plan,
    journal=sealed_journal,
    output_validations=output_results,
    payload_verifications=payload_results,
    evidence=additional_evidence,
    policy_findings=policy_findings,
    execution_errors=execution_errors,
    decision_links=decision_links,
    supervision_decisions=supervision_decisions,
    memory_records=memory_records,
    verifier_id="ELMAN_VERIFIER",
    requested_at="2026-08-08T00:00:00Z",
)

signer = FinalReportSigner(
    key_id="key:final-verification-v1",
    secret=protected_key_bytes,
)

report = FinalVerifier(request, signer).verify()
report.verify_signature(signer)

if report.status.value != "verified":
    raise RuntimeError("Final completion denied")
```

## Failure behaviour

The boundary fails closed when:

- input JSON, identifiers, timestamps or hashes are malformed;
- the plan, journal or an embedded result is tampered;
- output evidence cannot be reconstructed from a real journal prefix;
- a payload result references a missing output result;
- resolution evidence is missing, failed or under review;
- a memory decision link references the wrong revision;
- the report key is shorter than 32 bytes;
- a report hash, identifier or HMAC signature is invalid;
- any of the nine final gates fails.

Contract or integrity failures raise a typed exception. Valid but incomplete or
unsafe snapshots return a signed report whose status is `rejected` and whose
failed gates contain deterministic issue codes.

## Validation

The dedicated offline suite covers policy invariants, canonical round trips,
immutability, hash tampering, journal-prefix binding, evidence provenance,
resolution evidence, memory isolation, decision revision binding, HMAC key
handling, report tampering, all nine gates and signed rejection reports.

## Current boundaries

This increment does not yet:

- invoke final verification automatically from the orchestrator runtime;
- expose gate reports and evidence in ELMAN Studio;
- retrieve signing keys from an operating-system keystore or HSM;
- implement asymmetric signatures or external timestamping;
- validate external evidence payloads beyond their supplied hash-bound record;
- write reports to project memory automatically;
- authorize deployment or perform any artifact mutation.

Those integrations remain explicit work for Studio and stabilization. The
verifier itself grants no new execution authority.
