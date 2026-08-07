# ELMAN-OS v0.7 — Reflective Agent Enrichment

## Objective

Complete the roadmap-level reflective report without changing orchestration policy or applying any automatic correction.

The reflective agent now makes three previously implicit dimensions explicit:

- probable causes;
- hypotheses to verify;
- proposed improvements.

These dimensions are added alongside the existing fields:

- `what_worked`;
- `what_failed`;
- `evidence_gaps`;
- `recommended_correction`;
- `failure_fingerprint`.

## Contract compatibility

`ReflectionReport` keeps its existing required constructor arguments unchanged.

The new fields are appended with immutable empty-tuple defaults:

- `probable_causes: tuple[str, ...] = ()`
- `hypotheses_to_verify: tuple[str, ...] = ()`
- `proposed_improvements: tuple[str, ...] = ()`

Existing callers that construct a report with the former six positional arguments therefore remain valid.

## Deterministic reflection rules

The enrichment is derived only from `CycleResult` and the optional previous `CycleResult`.

No clock, random source, provider, model, network call, persistence layer, or mutable global state is consulted.

### Missing evidence

When a cycle contains no evidence:

- an evidence gap is recorded;
- a probable cause states that verifiable evidence is missing;
- a hypothesis notes that work may be insufficiently demonstrated;
- an improvement proposes attaching verifiable evidence to affected acceptance criteria.

### External blocker

When `blocked_reason` is present:

- the declared blocker becomes an explicit probable cause;
- the report proposes verifying whether an external dependency or human decision is required;
- the existing escalation correction is preserved and also exposed as a proposed improvement.

### Critical findings

When critical findings are present:

- the unresolved critical risk becomes an explicit probable cause;
- the report proposes verifying whether continuing automatically would be unsafe;
- the existing stop-and-escalate correction is preserved and exposed as a proposed improvement.

### Rework required

When the proof verdict is `REWORK_REQUIRED`:

- the report records that at least one proof gate or acceptance criterion remains unsatisfied;
- it proposes verifying whether an unresolved finding or insufficient proof explains the rework;
- the existing targeted rework correction is preserved and exposed as a proposed improvement.

### Acceptance criteria not validated

When criteria are not validated outside the previous cases:

- the missing validation becomes a probable cause;
- the report proposes verifying the highest-impact unsatisfied criterion;
- the existing targeted correction is preserved and exposed as a proposed improvement.

### No measurable progress

When a previous cycle exists and the current progress score does not improve:

- the current strategy is identified as a probable cause candidate;
- the report proposes the hypothesis that the correction does not address the root cause;
- the proposed improvement is to change one correction hypothesis at a time and measure its effect.

## Safety boundary

The reflective agent remains advisory.

It does not:

- modify production code;
- apply a correction;
- alter supervisor policy;
- approve a learning proposal;
- mutate orchestration state;
- persist a lesson;
- execute another agent;
- call an AI provider;
- access the network.

The report only exposes structured observations and proposals for downstream review.
