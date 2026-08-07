# ELMAN-OS v0.7 — Deterministic Metacognitive Confidence Report

## Status

This increment implements the roadmap item **rapport de confiance** for
Jalon 3 — Supervision métacognitive.

The report is deliberately conservative. It does not estimate an AI model's
subjective certainty and it does not claim that an execution result is correct.
It expresses **structural confidence in the metacognitive evidence that is
cryptographically bound to one supervision context**.

## Files

- `src/elman_os/metacognitive_confidence_report.py`
- `tests/test_metacognitive_confidence_report.py`
- `docs/METACOGNITIVE-CONFIDENCE-REPORT-v0.7.md`

No existing contract is modified by this increment.

## Architectural boundary

The reporter is read-only and declarative.

It does not:

- mutate an execution plan;
- append to or alter an execution journal;
- apply a metacognitive supervision decision;
- persist records;
- dispatch an agent;
- invoke an AI provider;
- access the network;
- approve a learning proposal;
- authorize delivery or production changes.

A confidence score is therefore **not an authorization token**.

## Existing contracts reused

The module reuses:

- `canonical_json`;
- `MetacognitiveSupervisionContext`;
- `MetacognitiveSupervisionFinding`;
- `MetacognitiveFindingKind`;
- the existing 0–10,000 basis-point confidence convention.

The existing supervision decision contract already uses basis points for
finding and decision confidence. This increment keeps the same representation.

## Confidence semantics

`overall_confidence_bp` ranges from `0` to `10_000`.

It measures confidence in the structural support for the supervision report,
not the probability that a final product is correct.

The score is the conservative minimum of:

1. the lowest confidence value among the bound findings;
2. the percentage of context evidence references covered by those findings;
3. any applicable uncertainty or evidence-gap cap.

The policy defaults are:

| Parameter | Default |
| --- | ---: |
| low threshold | 4000 bp |
| medium threshold | 6000 bp |
| high threshold | 8000 bp |
| uncertainty cap | 5999 bp |
| evidence-gap cap | 4999 bp |
| minimum findings | 1 |

The levels are:

- `insufficient`: score below 4000 bp;
- `low`: 4000–5999 bp;
- `medium`: 6000–7999 bp;
- `high`: 8000–10,000 bp.

Thresholds are policy data and are hash-bound.

## Why the minimum is used

Averages can hide a weak part of an evidence chain. For example, a 99% finding
and a 41% finding do not justify presenting the combined evidence as 70%
confident.

The confidence report therefore uses a floor rather than an average.

This is intentionally conservative and compatible with the v0.7 fail-closed
design.

## Evidence coverage

The supervision context already declares the evidence references on which the
observation is based.

The report computes:

```text
covered evidence references
---------------------------- × 10,000
context evidence references
```

using integer arithmetic.

A reference is covered only when it appears in a validated finding bound to the
same context.

No external evidence is discovered or fetched.

## Uncertainty and evidence-gap caps

A finding of kind `uncertainty` limits structural confidence to the configured
uncertainty cap.

A finding of kind `evidence-gap` applies the stricter evidence-gap cap.

If both are present, the lower cap wins.

Risk severity is intentionally not used as a confidence penalty. A critical
finding may be supported with very high confidence; risk and confidence are
different dimensions.

## No-findings behavior

An empty finding set produces:

- `finding_count = 0`;
- `evidence_coverage_bp = 0`;
- `overall_confidence_bp = 0`;
- `confidence_level = insufficient`.

Absence of a finding is not treated as evidence that the system is healthy.

## Determinism

The request embeds canonical JSON for:

- the confidence policy;
- the supervision context;
- every finding.

It binds those documents with SHA-256 hashes.

Findings are sorted by deterministic `finding_id`.

The report timestamp and author are inherited from the request, so generating a
report twice from the same request produces the same report identifier, content,
and hash.

There is no wall-clock read inside the reporter.

## Integrity checks

Deserialization fails closed when:

- an embedded policy hash is invalid;
- the context hash is invalid;
- a finding hash is invalid;
- a finding belongs to another context;
- duplicate findings are supplied;
- request hashes do not match;
- calculated metrics are modified;
- the confidence level does not match the score;
- covered evidence is modified;
- the deterministic author or timestamp is modified;
- the rationale is modified;
- the final report hash is invalid.

## Request contract

`MetacognitiveConfidenceReportRequest` binds:

- policy;
- supervision context;
- zero or more findings;
- requesting agent;
- request timestamp;
- reason.

The request timestamp cannot precede the supervision observation.

## Report contract

`MetacognitiveConfidenceReport` records:

- embedded request and request hash;
- finding count;
- context evidence count;
- covered evidence references;
- finding-confidence floor;
- evidence-coverage score;
- applicable cap;
- overall confidence score;
- confidence level;
- deterministic author and timestamp;
- deterministic rationale;
- SHA-256 report hash.

## Relationship to supervision decisions

The confidence report is informational.

It does not call the decision builder and does not select `continue`, `correct`,
`pause`, `stop`, or `escalate`.

A later integration increment may consume the report as evidence, but that
integration must preserve explicit policy and human-approval gates.

## Relationship to the roadmap

The v0.7 roadmap requires:

- a confidence level associated with results;
- a metacognitive confidence report;
- metacognitive reports visible to ELMAN Studio in a later milestone.

This increment implements only the confidence-report contract and deterministic
calculation.

Studio integration remains outside this increment.

## Explicit non-goals

This increment does not implement:

- automatic policy updates;
- automatic learning activation;
- reflective-agent enrichment;
- probabilistic calibration;
- LLM self-confidence;
- final-verifier signing;
- Studio rendering;
- persistence;
- network synchronization.

## Security posture

All inputs are local in-memory objects or serialized values supplied by the
caller.

The implementation performs no:

- file writes;
- process execution;
- dynamic imports;
- sockets;
- HTTP requests;
- provider calls;
- secret reads.

## Example

```python
request = MetacognitiveConfidenceReportRequest.capture(
    policy=policy,
    context=context,
    findings=findings,
    requested_by="SUPERVISOR_AGENT",
    requested_at="2026-08-07T01:30:00.000000Z",
    reason="Summarize confidence in bound metacognitive evidence.",
)

report = MetacognitiveConfidenceReporter().generate(request)

assert 0 <= report.overall_confidence_bp <= 10_000
report.verify_hash()
```

## Validation expectations

The increment is complete when:

1. the dedicated confidence-report tests pass;
2. the complete repository test suite passes;
3. `python -m elman_os release-check .` passes;
4. `git diff --check` reports no whitespace errors;
5. only the three expected repository files are added.
