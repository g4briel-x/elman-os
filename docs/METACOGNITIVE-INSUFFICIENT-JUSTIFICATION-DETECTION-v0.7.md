# ELMAN-OS v0.7 — Deterministic Insufficient-Justification Detection

## Purpose

This increment closes the roadmap gap requiring the metacognitive supervisor to
detect **insufficiently justified decisions**.

The implementation is deliberately deterministic and read-only. It does not
attempt to decide whether prose is persuasive, correct, or semantically valid.
It evaluates only explicit, auditable criteria configured by policy.

## Existing boundary

`MetacognitiveSupervisionDecision` already binds:

- the decision policy and its hash;
- the supervision context and its hash;
- embedded findings and finding hashes;
- the selected action;
- confidence;
- approval state;
- corrective step identifiers;
- decision author and timestamp;
- a non-empty rationale;
- the final decision hash.

The existing contract ensures structural and policy validity, but the rationale
can still be non-empty while failing to cite the evidence and records that
justify the action.

## New detector

`metacognitive_insufficient_justification_detection.py` adds a separate advisory
detector. It never alters the original decision.

The explicit policy can require:

- a minimum rationale length;
- a minimum number of exact context-evidence citations;
- exact citation of every embedded finding by finding ID or hash;
- exact citation of corrective step identifiers;
- an approval reference whenever approval is required;
- exact citation of that approval reference in the rationale.

These are deterministic string/contract checks. No natural-language semantic
inference is performed.

## Result model

A detection request is hash-bound to:

- the detector policy;
- the supervision decision;
- the decision context hash;
- requester, timestamp, and audit reason.

Each deficiency becomes an immutable `MetacognitiveJustificationGap` and one
existing `MetacognitiveSupervisionFinding` of kind `EVIDENCE_GAP`.

Possible gap kinds:

- `rationale-too-short`
- `insufficient-evidence-citations`
- `missing-finding-citation`
- `missing-corrective-step-citation`
- `missing-approval-reference`
- `missing-approval-reference-citation`

The result is either `sufficient` or `insufficient`.

## Security and governance

This detector:

- does not modify execution plans;
- does not modify orchestration state;
- does not modify decisions;
- does not persist state;
- does not dispatch agents;
- does not call AI providers;
- does not access the network;
- does not infer semantic justification;
- fails closed at the detector-policy boundary.

It therefore preserves ELMAN-OS separation between observation, finding,
decision, approval, and execution.
