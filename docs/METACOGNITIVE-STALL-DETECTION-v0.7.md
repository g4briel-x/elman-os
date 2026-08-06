# ELMAN-OS v0.7 — Metacognitive Stall Detection

## Status

This increment adds a deterministic, read-only detector for **active
orchestration stalls**. A stall is different from a loop:

- a loop requires a repeated contiguous event cycle;
- a stall requires sustained activity for a step without a measurable progress
  event;
- the detector can therefore identify a stalled step even when its event
  sequence is not cyclic.

The implementation is declarative. It produces immutable, canonical and
hash-bound evidence. It does not apply a metacognitive decision.

## Files

- `src/elman_os/metacognitive_stall_detection.py`
- `tests/test_metacognitive_stall_detection.py`
- `docs/METACOGNITIVE-STALL-DETECTION-v0.7.md`

## Detection model

The detector reads a validated `ExecutionJournal` bound to a
`MetacognitiveSupervisionContext`.

For each step, it retains the active suffix of step-scoped events after the
last `step.completed` event. A step is reported as stalled when:

1. the active suffix reaches `minimum_activity_events`;
2. the suffix covers at least `minimum_sequence_span` journal positions;
3. the plan has not subsequently reached `plan.completed` or `plan.failed`;
4. the journal still matches the hash-bound detection request.

`step.completed` is the measurable progress signal for this increment.
A terminal plan event clears all active stall candidates.

## Contracts

### `MetacognitiveStallDetectionPolicy`

Defines deterministic thresholds:

- minimum activity events;
- maximum retained window size;
- minimum journal sequence span;
- high and critical risk thresholds;
- base confidence and deterministic confidence increment;
- fail-closed enforcement.

### `MetacognitiveStallDetectionRequest`

Binds the operation to:

- the complete policy and its SHA-256 hash;
- the metacognitive supervision context and its hash;
- the journal plan identifier;
- the journal event count;
- the journal head hash;
- the journal integrity hash;
- one evidence reference already present in the context;
- requester, timestamp and reason.

### `MetacognitiveStallWindow`

Captures one active stalled step:

- step identifier;
- first and last journal sequence;
- activity-event count;
- full journal sequence span;
- ordered event types;
- participating agent identifiers;
- deterministic risk level;
- deterministic confidence;
- evidence references;
- canonical identity and content hashes.

### `MetacognitiveStallDetectionRecord`

Pairs a stall window with a
`MetacognitiveSupervisionFinding(kind="stall")`. The record verifies that the
finding exactly matches the window's context, risk, confidence, affected step,
evidence and canonical summary.

### `MetacognitiveStallDetectionResult`

Contains either:

- `clear`, with no records; or
- `stalls-detected`, with one or more records.

The result is bound to the request and records by canonical SHA-256 hashes.

## Risk mapping

With the default policy:

| Active non-progress events | Risk |
|---:|---|
| 4–6 | medium |
| 7–9 | high |
| 10+ | critical |

Risk thresholds are configurable but must remain monotonic.

## Confidence mapping

Default confidence begins at 6500 basis points for the minimum accepted window.
Each additional retained activity event adds 350 basis points, capped at 10000.

The calculation is deterministic and contains no probabilistic or AI-based
inference.

## Integrity guarantees

The implementation fails closed when:

- policy thresholds are inconsistent;
- policy, context, request, window, record or result hashes do not match;
- journal plan, event count, head hash or integrity hash differs from the
  request;
- an evidence reference is not bound to the context;
- a stall window contains a `step.completed` event;
- a record's finding differs from its stall window;
- completion timestamps precede request timestamps;
- the journal or context changes during detection.

## Side-effect boundary

The detector performs none of the following:

- execution-plan mutation;
- execution-journal mutation;
- state persistence;
- decision application;
- agent dispatch;
- AI-provider invocation;
- network access;
- Git commit, push, pull request or tag creation.

## Validation

The dedicated test module covers:

- policy invariants;
- canonical serialization;
- request/context/journal binding;
- deterministic identifiers and hashes;
- minimum, high and critical thresholds;
- confidence growth and cap;
- completed-step and terminal-plan resets;
- multiple independent stalled steps;
- maximum window retention;
- finding construction;
- result and record round trips;
- mismatched-journal rejection;
- timestamp enforcement;
- read-only behavior;
- deterministic repeated execution.

The bundle validation script runs:

1. Python compilation;
2. 43 dedicated tests;
3. the complete repository test suite;
4. `python -m elman_os release-check .`;
5. an exact Git status check for the three expected untracked files.
