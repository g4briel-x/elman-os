# ELMAN-OS v0.7 — Metacognitive Missing-Dependency Detection

## Objective

Add deterministic detection of required dependency relations that are absent
from an otherwise structurally valid `ExecutionPlan`.

`ExecutionPlan` already rejects unknown dependency identifiers, self-dependency,
dependency cycles, and active steps whose declared prerequisites are incomplete.
This increment therefore does not duplicate those invariants.

## Explicit dependency policy

The detector consumes explicit dependency requirements. Each requirement names:

- a dependent step;
- a prerequisite step;
- a relation mode: `direct` or `transitive`.

`direct` requires the prerequisite to be present in the dependent step's
`dependencies` tuple. `transitive` accepts the prerequisite anywhere in the
validated dependency ancestry.

No dependency is inferred from titles, objectives, capability names, prose, AI
output, or heuristics.

## Fail-closed boundary

If the dependency policy references a step that does not exist in the supplied
plan, the detector fails closed instead of inventing a dependency finding. A
missing step belongs to incomplete-plan detection, not missing-edge detection.

## Findings

Each absent required dependency produces:

- one immutable, hash-bound missing-dependency gap record;
- one existing `MetacognitiveSupervisionFinding` with kind `evidence-gap`;
- affected step identifiers for both the dependent and prerequisite step;
- evidence bound to the metacognitive supervision context.

## Integrity

The request binds:

- the explicit dependency policy and its hash;
- the metacognitive supervision context and its hash;
- canonical `ExecutionPlan.to_json()`;
- the SHA-256 plan-state hash;
- a plan evidence reference;
- requester, timestamp, and reason.

The result is deterministic and hash-bound to the request, gaps, and findings.

## Safety

The detector is read-only. It does not:

- add, remove, reorder, or modify plan steps;
- add dependency edges automatically;
- infer dependencies semantically;
- alter orchestration state;
- persist state;
- dispatch agents;
- approve plans or steps;
- invoke an AI provider;
- access the network.

This increment addresses missing dependency relations only. Insufficiently
justified decisions and drift from the initial intent remain separate Jalon 3
concerns.
