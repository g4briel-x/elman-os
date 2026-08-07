# ELMAN-OS v0.7 — Metacognitive Incomplete-Plan Detection

## Objective

Add deterministic detection of incomplete execution plans to the metacognitive
supervision layer without duplicating the structural invariants already
enforced by `ExecutionPlan`.

A structurally valid plan can still omit required work. Semantic completeness
cannot be inferred safely from prose, so this detector compares the plan only
against explicit completeness requirements.

## Explicit completeness policy

The policy can declare:

- required step identifiers;
- required capability identifiers;
- a minimum step count;
- whether every step must already have an agent binding;
- whether human approvals must have an effective approval trace.

## Detected gaps

- `minimum-step-count`
- `missing-required-step`
- `missing-required-capability`
- `unbound-step`
- `missing-plan-approval`
- `missing-step-approval`

Each gap emits one existing `MetacognitiveSupervisionFinding` with kind
`evidence-gap`. This keeps the shared decision-contract enum unchanged while
making the incomplete-plan result explicit and consumable by the existing
supervision layer.

## Integrity

The request binds the policy, supervision context, canonical execution plan,
plan-state hash, evidence reference, requester, timestamp, and reason.

The plan-state hash is SHA-256 of canonical `ExecutionPlan.to_json()`, matching
the checkpoint convention already used by ELMAN-OS.

## Determinism and safety

The detector is read-only and deterministic. It does not:

- guess semantic requirements;
- modify a plan;
- add or remove steps;
- assign agents;
- approve plans or steps;
- mutate orchestration state;
- persist state;
- dispatch agents;
- call an AI provider;
- access the network.

This increment addresses incomplete-plan detection only. Missing dependency
requirements, insufficient decision justification, and intent drift remain
separate Jalon 3 concerns.
