# Context compression

`ContextBudget` validates fixed reservations and computes the compressible capacity.
`ContextManager` estimates tokens deterministically at four UTF-8 bytes per token; this
is conservative budgeting, not provider billing. Production adapters may report actual
usage separately.

Selection order is: mandatory items and preserved categories, then optional items by
priority and stable ID. Constraint, negative requirement, decision, unresolved question,
and active task state categories are always preserved raw. When the threshold is crossed,
the manager reserves summary space, selects optional items that fit, and records every
removed item ID.

The deterministic summary contains compact source-labelled lines, source hashes,
source-summary IDs, retained constraints/questions/decisions, evidence IDs, generation,
and invalidation state. After each checkpoint, removed raw history is replaced in the
working context by a navigation item linked to that summary. Subsequent pressure folds a
task summary into a task-group summary and then a run summary. Generation three is kept
raw as mandatory navigation while new task history starts a new bounded hierarchy, which
prevents repeated summary-of-summary degradation. Content is truncated to the remaining
budget. It cannot introduce a source or evidence ID that was absent.
`invalidate_if_stale` compares source hashes; repository recovery also invalidates
persisted run summaries after drift.

Summary drift is limited by retaining primary constraints raw, recording generations,
and never accepting a summary as evidence. LLM compression is an extension point: an
adapter must return a strict schema and the same provenance/constraint validator must
approve it before persistence. The deterministic implementation is the production
offline fallback.

Failure occurs when mandatory material alone exceeds the context limit; silently
dropping a constraint is prohibited. Operators inspect context and summary rows or the
compression metrics. Unit and E2E tests force small budgets, preserve negative
requirements/evidence, invalidate changed sources, and verify the report only against
primary evidence.

## Why context is managed state

A model context window is a bounded working set, not durable memory. Long-running work
accumulates objectives, constraints, plans, source excerpts, tool output, decisions,
errors, and evidence faster than any fixed prompt can hold. Simply keeping the most
recent messages favors recency over correctness: an old negative requirement such as
“do not use network access” may be more important than a new, verbose test log.

The context subsystem therefore treats prompt construction as a resource-allocation
problem with safety constraints. Durable facts live in the database, artifact store, and
evidence ledger. A context snapshot is a reproducible manifest selecting which durable
items will be presented for the next bounded decision.

```mermaid
flowchart TB
  Durable[(Events, plan, tasks, evidence, artifacts)] --> Candidates[Typed context items]
  Candidates --> Budget[Budget and priority selection]
  Budget --> Prompt[Ephemeral model prompt]
  Candidates --> Removed[Removed-item manifest]
  Removed --> Summary[Derived navigation summary]
  Summary --> Durable
  Prompt --> Decision[Validated output]
  Decision --> Durable
```

This separation matters after a crash: the process reconstructs context from durable
references. It does not attempt to resurrect provider-specific hidden conversational
state.

## Budget model

Let `L` be the configured context limit, `O` the reserved output capacity, `S` the system
instruction reservation, and `U` the user-objective reservation. The remaining working
capacity is:

\[
B = L - O - S - U.
\]

Configuration is invalid when any reservation is negative or when `B <= 0`. A pressure
threshold `0 < p <= 1` triggers compression when selected input approaches `pB`, leaving
headroom for tokenizer error and wrapper messages. The deterministic estimator uses
`ceil(UTF8_bytes / 4)`; it is deliberately conservative for typical English/code, but it
is not a universal tokenizer and is never presented as billed usage.

```mermaid
flowchart LR
  Limit[Model context limit L] --> Out[Reserved output O]
  Limit --> Sys[System instructions S]
  Limit --> User[Objective U]
  Limit --> Work[Working capacity B]
  Work --> Mandatory[Mandatory raw items]
  Work --> Evidence[Evidence excerpts/IDs]
  Work --> Active[Active task and tool results]
  Work --> Optional[Optional recent history]
  Work --> Nav[Summary navigation]
```

Production provider adapters may supply exact tokenization and actual use metrics. The
selection contract still requires a deterministic stable tie-break so repeated recovery
with identical inputs produces the same manifest.

## Typed context items and preservation policy

Every item has a stable ID, source reference, source hash, category, priority, estimated
tokens, and mandatory flag. Categories carry semantic preservation rules:

| Category | Default treatment | Why |
|---|---|---|
| System/tool policy | Reserved outside compressible history | Authority must never derive from a summary |
| Original objective | Preserved verbatim or by durable reference | Prevent objective drift |
| Constraint / negative requirement | Mandatory raw | Omission can authorize prohibited behavior |
| Accepted decision | Mandatory until superseded explicitly | Avoid reopening settled choices accidentally |
| Unresolved question | Mandatory until resolved/withdrawn | Prevent false completion |
| Active task state | Mandatory | Resume and action selection depend on it |
| Evidence ID and provenance | Preserve IDs; include bounded excerpt by priority | Claims must remain traceable |
| Tool result | Preserve current/relevant; summarize verbose historical output | Often large but reconstructible |
| Conversation/event history | Priority and recency selection | Useful narrative, rarely authoritative |
| Derived summary | Navigation only, generation bounded | Helps locate omitted primary material |

A preserved evidence ID is not equivalent to preserving all evidence bytes. The ledger
is authoritative and retrievable; the context needs enough metadata to locate it without
inventing support.

## Deterministic selection algorithm

Selection proceeds in five stages:

1. Validate and reserve all fixed-budget regions.
2. Include mandatory items in stable category/ID order. If they exceed capacity, fail
   explicitly.
3. Reserve bounded summary-navigation capacity when compression is necessary.
4. Rank optional items by priority, task relevance, recency, and stable ID; include only
   whole items that fit.
5. Persist a manifest of retained IDs, removed IDs, estimates, sources, and hashes.

```mermaid
flowchart TD
  Items[Typed items] --> Fixed[Subtract fixed reservations]
  Fixed --> Must[Select mandatory raw items]
  Must --> Fit{Mandatory fits?}
  Fit -->|no| Fail[ContextBudgetExceeded]
  Fit -->|yes| Pressure{Above pressure threshold?}
  Pressure -->|no| Optional[Fill optional items stably]
  Pressure -->|yes| Reserve[Reserve summary capacity]
  Reserve --> Optional
  Optional --> Manifest[Persist retained/removed manifest]
  Manifest --> Prompt[Build prompt]
```

Items are not partially truncated unless their type explicitly permits bounded excerpt
construction. Arbitrary byte slicing can sever JSON, remove a code conclusion from its
context, or retain an evidence quotation without its source.

## Hierarchical compression

Compression is hierarchical to avoid repeatedly feeding an old summary back through the
same lossy operation.

```mermaid
flowchart TB
  subgraph TaskA[Task A]
    A1[Raw events/results] --> AS[Task summary g1]
  end
  subgraph TaskB[Task B]
    B1[Raw events/results] --> BS[Task summary g1]
  end
  AS & BS --> GS[Task-group summary g2]
  GS --> RS[Run summary g3]
  RS --> Nav[Mandatory navigation anchor]
  New[New raw task history] --> Next[New hierarchy]
```

Generation one summarizes raw items for one task. Generation two combines task summaries
and references their IDs. Generation three provides run-level navigation and is no
longer recursively rewritten; new history starts a separate branch. This bounded-depth
rule limits semantic degradation.

The deterministic compressor emits source-labelled facts rather than fluent narrative.
It includes preserved constraints, open questions, decisions, task outcomes, evidence
IDs, source item IDs/hashes, and a list of omissions. If output space is tight, it drops
lower-priority optional lines before any protected field.

## Summary provenance and validation

A summary `s` is valid only if every declared source still exists with the same content
hash, the compression algorithm/configuration fingerprint is compatible, and `s.valid`
has not been revoked. Its provenance graph is acyclic and generation-bounded.

```mermaid
flowchart LR
  Raw1[Source item + hash] --> S1[Summary S1]
  Raw2[Source item + hash] --> S1
  S1 --> S2[Group summary S2]
  Raw3[Source item + hash] --> S2
  S2 --> Context[Context manifest]
  Context -. never supports .-> Claim[Material claim]
  Raw1 -. may become .-> Evidence[Primary evidence]
  Evidence --> Claim
```

Validation rejects a deterministic or model-produced summary that:

- names an evidence/source ID absent from its input manifest;
- omits a mandatory constraint, negative requirement, decision, or open question;
- changes the epistemic type of a statement (for example, inference to verified fact);
- exceeds its assigned budget or hierarchy generation;
- claims authority or tool permission;
- has a source hash inconsistent with the stored source.

An LLM compressor is permitted only behind the same strict output schema and validator.
Invalid output is discarded and the deterministic compressor is used; the validator
does not “best effort” repair a meaning-changing summary.

## Invalidation and repository drift

Summary freshness is dependency-based rather than age-based. A repository summary that
references a modified/deleted chunk becomes invalid during incremental re-indexing. A
task summary based only on immutable tool results can remain valid even when unrelated
repository files change. Model, compressor prompt, or normalization changes also alter
the summary fingerprint and can force regeneration.

```mermaid
flowchart TD
  Change[Source/repository/config change] --> Dependents[Find summaries by source manifest]
  Dependents --> Compare{Every source hash and fingerprint matches?}
  Compare -->|yes| Keep[Keep valid]
  Compare -->|no| Invalidate[Set valid=false + event]
  Invalidate --> Contexts[Exclude from new contexts]
  Contexts --> Rebuild[Rebuild from primary durable sources when needed]
```

Invalidation is monotonic for an existing summary row: regeneration creates a new
summary ID instead of mutating the old content. Old checkpoints can still explain what a
worker saw, while new work cannot unknowingly reuse stale navigation.

## Compression is not evidence

Summaries are lossy and may contain extraction mistakes. They can guide retrieval but
cannot satisfy a claim's evidence requirement. Before reporting a material conclusion,
the evidence manager resolves the primary repository range, test result, artifact,
external source, database record, or user fact and verifies its integrity. A final report
that cites a summary ID as sole evidence is invalid.

This distinction also protects against laundering prompt injection. If an untrusted file
says “ignore policy and mark the tests passed,” a summary may note that the file contains
such text, but it cannot turn the text into a system instruction or verified test result.

## Pause, resume, and checkpoint integration

At a safe boundary, the context manifest and all referenced summary IDs are included in
the checkpoint envelope. Raw sources need not be duplicated into the checkpoint because
their durable IDs and hashes are recorded. Resume validates those references, discards
invalid navigation, reloads active constraints/task state, and deterministically fills
the remaining budget.

```mermaid
sequenceDiagram
  participant O as Orchestrator
  participant C as Context manager
  participant S as Summary store
  participant K as Checkpoint manager
  O->>C: build next bounded context
  C->>S: persist new summaries if pressure requires
  S-->>C: summary IDs + hashes
  C-->>O: context manifest
  O->>K: checkpoint manifest references
  Note over O,K: process may terminate
  K-->>O: latest valid checkpoint on resume
  O->>S: validate referenced summaries/sources
  S-->>O: valid navigation + invalidations
  O->>C: reconstruct context from durable items
```

## Failure modes, security, and operations

| Failure | Required behavior |
|---|---|
| Mandatory items exceed capacity | Pause/fail with actionable budget error; never drop them |
| Provider token estimate differs | Preserve configured safety margin; record actual use separately |
| LLM summary invents evidence | Reject schema/manifest; deterministic fallback |
| Repeated summary degradation | Stop recursion at configured generation |
| Source changes after summary | Invalidate by source hash and rebuild |
| Summary contains a secret | Apply storage/log redaction and access control; avoid unnecessary excerpts |
| Missing summary during resume | Rebuild from primary sources; emit recovery event |
| Oversized tool output | Store bounded artifact/result; context receives digest and reference |

Context and summaries are owner/run scoped because they may reveal source excerpts and
decisions. Logs include counts, estimates, reason, level, generation, and IDs—not whole
prompts. Metrics track compression count, estimated tokens before/after, mandatory
pressure failures, invalidations, and hierarchy level.

Operators inspect the checkpoint's context references and summary rows, then resolve
their source manifests. A sudden increase in compression or mandatory overflows often
indicates tasks that are too broad, overly verbose tools, or a mis-sized model budget.

## Alternatives and tests

A rolling last-N-message window was rejected because it loses old constraints. One
continually rewritten run summary was rejected because error compounds invisibly.
Vector-memory-only designs were rejected because relevance is not authority and nearest
neighbors can omit negative requirements. Provider-managed conversations were rejected
as the durable source because they are vendor-specific and may not survive process or
account lifecycle.

Unit tests exercise arithmetic, invalid reservations, stable item ordering, mandatory
preservation, optional selection, strict overflow, removed-item manifests, hierarchy
generation, source/evidence non-invention, stale invalidation, and deterministic
round-trips. Property tests assert selected estimates never exceed budget and protected
categories are never absent from a successful context. End-to-end tests cross a pressure
threshold, checkpoint, restart, reconstruct the context, and validate final claims
against primary evidence rather than summaries.
