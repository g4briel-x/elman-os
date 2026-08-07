# ELMAN-OS v0.7 — Deterministic Metacognitive Contradiction Detection

## Status

This increment implements the roadmap item **détection de contradiction** for
Jalon 3 — Supervision métacognitive.

The implementation is deliberately conservative. It reports a contradiction
only when the execution journal contains either:

1. two or more explicit assertions with the same scope, subject, and predicate
   but distinct canonical JSON values; or
2. a step that emits `step.blocked` or `step.failed` after it has already
   emitted `step.completed`.

It does not infer semantic contradiction from free-form text.

## Files

- `src/elman_os/metacognitive_contradiction_detection.py`
- `tests/test_metacognitive_contradiction_detection.py`
- `docs/METACOGNITIVE-CONTRADICTION-DETECTION-v0.7.md`

## Architectural boundary

The detector is read-only and declarative.

It does not:

- mutate an execution plan;
- append to or alter an execution journal;
- apply a metacognitive supervision decision;
- persist records;
- dispatch an agent;
- invoke an AI provider;
- access the network;
- infer truth from natural-language output.

The execution journal and supervision context are serialized before and after
analysis. Any detected mutation causes a fail-closed integrity error.

## Existing contracts reused

The module reuses:

- `ExecutionJournal`, `ExecutionEvent`, and `ExecutionEventType`;
- `MetacognitiveSupervisionContext`;
- `MetacognitiveSupervisionFinding`;
- `MetacognitiveFindingKind.CONTRADICTION`;
- `MetacognitiveRiskLevel`;
- `canonical_json`.

No existing contract is modified.

## Explicit assertion envelope

Assertions are carried in an execution-event payload under the default key:

```json
{
  "metacognitive_assertions": [
    {
      "scope": "scope:release-candidate",
      "subject": "artifact:bundle",
      "predicate": "approved",
      "value": true,
      "step_id": "verification"
    }
  ]
}
```

Required fields:

- `scope`: the logical snapshot or decision boundary within which assertions
  must agree;
- `subject`: the entity being described;
- `predicate`: the property being asserted;
- `value`: any finite JSON value.

Optional field:

- `step_id`: an affected orchestration step. When the source event already has
  a `step_id`, the supplied value must match it.

Unknown fields, malformed envelopes, non-JSON values, and oversized assertion
arrays fail closed.

## Why scope is mandatory

A value may legitimately change over time. For example, an artifact may be
`approved: false` before review and `approved: true` after review. These are not
contradictory when their scopes differ.

Only assertions within the same explicit scope are compared. This prevents the
detector from confusing revision history with logical contradiction.

## Canonical comparison

Assertion values are converted to canonical JSON before comparison.

The following values are therefore equivalent:

```json
{"a": 1, "b": 2}
```

```json
{"b": 2, "a": 1}
```

Object key order cannot create a false contradiction.

## Completed-step regression

A step may recover from `step.blocked` or `step.failed` and later complete.
Those transitions are not reported.

The detector reports only the reverse transition:

```text
step.completed
    ↓
step.blocked or step.failed
```

Such a regression contradicts the already recorded completion state.

## Immutable contracts

The increment introduces the following immutable, hash-bound contracts:

### `MetacognitiveContradictionDetectionPolicy`

Controls:

- assertion payload key;
- maximum assertions per event;
- risk thresholds;
- confidence increments;
- assertion-conflict detection;
- completed-step regression detection;
- mandatory fail-closed behavior.

### `MetacognitiveAssertion`

Binds a canonical assertion to:

- scope;
- subject;
- predicate;
- canonical JSON value;
- source event sequence;
- source event hash;
- optional step;
- optional agent.

### `MetacognitiveContradiction`

Records:

- contradiction kind;
- logical scope;
- subject and predicate;
- distinct canonical values;
- source event sequences and hashes;
- source assertion hashes;
- affected steps;
- risk;
- confidence.

### `MetacognitiveContradictionDetectionRequest`

Binds the analysis to:

- policy hash;
- supervision context hash;
- journal plan identifier;
- journal event count;
- journal head hash;
- journal seal hash;
- context-bound evidence reference;
- requesting agent and timestamp.

### `MetacognitiveContradictionDetectionResult`

Embeds and verifies:

- the request;
- extracted assertions;
- contradictions;
- `CONTRADICTION` findings;
- result status;
- deterministic analysis identity and hash.

## Contradiction kinds

### `assertion-conflict`

The same `(scope, subject, predicate)` contains at least two distinct canonical
JSON values.

### `completed-step-regression`

A step produces `step.blocked` or `step.failed` after `step.completed`.

## Risk classification

For assertion conflicts:

- 2 distinct values: `medium`;
- 3 distinct values: `high`;
- 4 or more distinct values: `critical`.

Thresholds are policy-controlled.

Completed-step regressions are `high` risk because they invalidate an already
recorded completion state.

## Confidence

Default confidence begins at 7000 basis points and increases deterministically
with:

- additional source events;
- additional distinct values.

Confidence is capped at 10000 basis points.

## Finding production

Each contradiction produces exactly one
`MetacognitiveSupervisionFinding` with:

- `kind = contradiction`;
- the contradiction risk level;
- deterministic summary text;
- the journal evidence reference already bound to the context;
- affected step identifiers;
- deterministic confidence.

The detector does not convert the finding into `continue`, `correct`, `pause`,
`stop`, or `escalate`. Decision application remains outside this increment.

## Determinism

For identical:

- policy;
- context;
- journal;
- request metadata;
- analyzing agent;

the detector produces byte-identical canonical JSON and identical SHA-256
identifiers.

The analysis timestamp is the request timestamp. No wall-clock read is used.

## Fail-closed guarantees

Detection fails when:

- policy integrity is invalid;
- context integrity is invalid;
- the journal fails validation;
- journal bindings differ from the request;
- the evidence reference is not bound to the context;
- an assertion envelope is malformed;
- an assertion contains unsupported fields;
- a source event hash is invalid;
- embedded assertions, contradictions, or findings are altered;
- the journal or context changes during analysis.

## Test coverage

The dedicated suite contains 37 tests covering:

- policy validation and immutability;
- request, assertion, contradiction, and result round trips;
- hash-tamper rejection;
- explicit assertion conflicts;
- scope separation;
- canonical JSON equivalence;
- risk classification;
- malformed-envelope fail-closed behavior;
- assertion limits;
- step binding;
- normal recovery transitions;
- completed-step regressions;
- detector feature switches;
- deterministic output;
- journal and context non-mutation;
- request-to-journal binding;
- one finding per contradiction.

## Validation commands

```powershell
python -m py_compile `
    .\src\elman_os\metacognitive_contradiction_detection.py `
    .\tests\test_metacognitive_contradiction_detection.py

python -m unittest discover `
    -s tests `
    -p "test_metacognitive_contradiction_detection.py" `
    -v

python -m unittest discover -s tests -v

python -m elman_os release-check .
```

## Production gate

This increment remains a supervision-analysis primitive only.

It must not be connected to automatic correction, pause, stop, escalation,
persistence, or agent execution until those boundaries receive their own
reviewed and tested increments.
