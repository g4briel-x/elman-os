# ELMAN-OS v0.7 — Structured Project Memory

## Purpose

This increment implements roadmap milestone 4: a local, structured and
traceable project memory. It preserves validated knowledge across executions
without allowing silent edits to historical decisions.

The boundary is deterministic and offline. It does not call AI providers,
dispatch agents, modify project files, or access the network.

## Stored knowledge

`ProjectMemoryKind` distinguishes seven explicit categories:

- `decision`;
- `constraint`;
- `convention`;
- `test-result`;
- `migration`;
- `incident`;
- `source-of-truth`.

Every revision is scoped by:

- tenant;
- project;
- optional execution;
- stable memory identifier;
- revision number;
- kind and state;
- retention class;
- immutable provenance;
- payload hash;
- previous revision hash;
- revision hash.

## Provenance

`ProjectMemoryOrigin` records:

- a bounded source type;
- a source identifier;
- the actor that captured the memory;
- an ISO-8601 UTC timestamp;
- zero or more evidence references.

Decision entries are stricter than other memory kinds. They require:

- `user-approval` provenance;
- at least one evidence reference;
- permanent retention.

This contract records the approval provenance. It does not independently
validate an approval database; that integration remains a later orchestration
increment.

## Immutable decisions and revision history

SQLite triggers reject updates and deletions of revision metadata.

A non-decision entry can receive a new revision only when the caller supplies
the exact current revision number. The new revision contains the preceding
revision hash, producing an auditable chain.

Terminal states are:

- `superseded`;
- `obsolete`.

Terminal memory cannot be silently reactivated.

A decision cannot be revised in place. A changed decision must be recorded as
a new permanent decision whose `supersedes_memory_id` references the previous
decision. Searches return the current decision by default while preserving the
complete history on request.

## Local structured storage

`ProjectMemoryStore` uses a file-backed SQLite database with:

- an explicit schema version;
- WAL journaling;
- foreign-key enforcement;
- bounded busy timeouts;
- owner-only file permissions on POSIX systems;
- append-only revision and retention-event tables;
- a separate payload table.

The separate payload table is important: retention can remove expired content
without removing the immutable revision, content hash or provenance.

Payload absence is accepted only when a corresponding immutable retention
event exists. Direct deletion of a payload is therefore detected as an
integrity failure.

## Search

Search is always scoped by tenant and project. It can additionally filter by:

- execution identifier;
- one or more memory kinds;
- current or inactive state;
- current or superseded knowledge;
- case-insensitive literal text in title, content and labels;
- bounded limit and offset.

Queries use SQL parameters and escape wildcard characters. Search does not use
semantic inference, embeddings, remote indexes or AI providers.

## Retention

`ProjectMemoryRetentionPolicy` is immutable and fail-closed.

Four retention classes exist:

| Class | Behaviour |
|---|---|
| `permanent` | no automatic payload expiration |
| `project` | retained until an explicit future project-lifecycle policy exists |
| `execution` | expires after the configured execution duration |
| `transient` | expires after the configured short duration |

Additional rules prevent unsafe classification:

- decisions are always permanent;
- constraints, conventions, migrations and sources of truth cannot use an
  expiring class;
- execution retention requires an execution identifier.

`apply_retention()` deletes only eligible payload rows. For every deletion it
first appends a hash-bound retention event containing the tenant, project,
memory, revision, content hash, policy and purge time. The transaction is
atomic and idempotent.

## Secret exclusion

All payloads and provenance are checked before persistence.

The boundary rejects:

- sensitive field names such as password, credential, private key, API key,
  authorization, cookie, session and token fields;
- private-key markers;
- common GitHub, OpenAI and Slack credential formats;
- bearer/basic authorization values;
- credential assignment patterns;
- JWT-shaped values;
- non-finite JSON numbers;
- non-JSON values;
- payloads larger than 1 MiB.

Tests construct credential fixtures dynamically so the repository itself does
not contain release-blocking credential markers.

This scanner is a deterministic safety gate, not a complete DLP product. The
database is not encrypted by this increment. Production environments must
still use protected local storage, operating-system access controls and a
dedicated secret manager.

## Minimal example

```python
from elman_os.project_memory import (
    ProjectMemoryKind,
    ProjectMemoryOrigin,
    ProjectMemoryRetentionClass,
    ProjectMemorySourceType,
    ProjectMemoryStore,
)

store = ProjectMemoryStore(".elman/project-memory.sqlite3")

record = store.record(
    tenant_id="tenant:local",
    project_id="project:elman-os",
    execution_id="execution:memory-milestone",
    kind=ProjectMemoryKind.DECISION,
    title="Adopt append-only project memory",
    content={
        "decision": "Use hash-chained SQLite revisions.",
        "status": "approved",
    },
    labels=("architecture", "memory"),
    origin=ProjectMemoryOrigin(
        source_type=ProjectMemorySourceType.USER_APPROVAL,
        source_id="approval:memory-architecture",
        actor_id="human:project-owner",
        captured_at="2026-08-07T04:00:00Z",
        evidence_references=("evidence:roadmap-v070",),
    ),
    retention_class=ProjectMemoryRetentionClass.PERMANENT,
)

record.verify_hash()
```

## Failure behaviour

The store fails closed when:

- a contract is malformed;
- a secret pattern is detected;
- decision provenance is insufficient;
- a caller attempts to revise a decision;
- an optimistic revision is stale;
- a revision chain is broken;
- metadata or payload hashes do not match;
- a payload disappears without retention evidence;
- a retention policy is unsafe;
- SQLite cannot complete the requested transaction.

## Validation

The dedicated suite covers contracts, persistence, reopening, concurrency,
tenant/project isolation, revisions, immutable decisions, supersession,
search, retention, secret exclusion, SQLite immutability triggers and tamper
detection. All tests are local and use temporary databases.

## Current boundaries

This increment does not yet:

- connect the memory store to the multi-agent orchestration runtime;
- validate approval references against the approval store;
- expose memory in ELMAN Studio;
- encrypt SQLite payloads at rest;
- implement semantic/vector search;
- expire `project` memory automatically when a project closes;
- synchronize memory between machines or services.

Those boundaries remain explicit so milestone 4 does not silently grant new
execution authority.
