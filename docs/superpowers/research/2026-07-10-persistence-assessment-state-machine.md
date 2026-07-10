# Persistence Assessment State Machine

- **Status:** Normative CS-14 implementation authority
- **Date:** 2026-07-10
- **Scope:** Deterministic passive assessment, operation results, restart
  recovery, and exit mapping for account persistence
- **Integrated by:**
  [architecture design](../specs/2026-07-09-maintainable-application-architecture-design.md)
  and
  [implementation plan](../plans/2026-07-09-maintainable-application-architecture.md)

## 1. Outcome

Persistence assessment is a phased reduction over closed evidence. It is not a
single cross-product of every possible file combination.

The assessor:

1. qualifies the parent filesystem;
2. enumerates only the authority and exact managed artifact grammar;
3. validates object type, no-follow state, link count or reparse state,
   ownership, and permissions;
4. bounded-reads safe relevant objects;
5. decodes the authority and structured artifacts;
6. verifies digest-derived artifact names;
7. derives authority/artifact relations;
8. sorts every issue by fixed precedence; and
9. exposes the first issue as primary while retaining all issues safely.

Unknown sibling filenames are never opened, reported, cleaned, or deleted.

## 2. Closed evidence

```python
class AuthorityKind(StrEnum):
    ABSENT = "absent"
    GENERATION_ZERO = "generation_zero"
    VERSION_ONE = "version_one"
    FUTURE = "future"
    DUPLICATE_KEY = "duplicate_key"
    MALFORMED_JSON = "malformed_json"
    INVALID_SCHEMA = "invalid_schema"
    UNREADABLE = "unreadable"
    UNSAFE = "unsafe"
    UNSUPPORTED_FILESYSTEM = "unsupported_filesystem"


class ArtifactKind(StrEnum):
    LOCK = "lock"
    V0_BACKUP = "v0_backup"
    V1_SNAPSHOT = "v1_snapshot"
    PROTOTYPE_RECEIPT = "prototype_receipt"
    TEMPORARY = "temporary"
    PROTOTYPE = "prototype"
```

Internally, validated artifacts retain only the facts needed to classify
relations: kind, safe basename, digest, bounded bytes, decoded generation, and
account count. Collections are sorted lexicographically by basename before
classification. Account insertion order inside decoded documents is never
sorted.

## 3. Artifact validity

A v0 backup is valid only when:

- its basename exactly matches the managed v0 grammar;
- it is a protected readable regular single-link or non-reparse object;
- its basename digest equals SHA-256 of exact bytes; and
- its bytes decode as strict generation zero.

A v1 snapshot is valid only when the corresponding checks pass, it decodes as
strict version one, and its bytes equal canonical deterministic version-one
serialization.

Multiple different valid backups are expected history, not a conflict.

Managed backup failures map to:

- `unsafe_permissions` for unsafe object or security state;
- `unreadable` when a safe object cannot be bounded-read; and
- `backup_conflict` for digest mismatch, wrong generation, malformed or
  invalid content, or noncanonical version-one bytes.

`malformed_json` and `duplicate_key` are authority or actively eligible
prototype codes. A malformed backup is `backup_conflict` with only its safe
basename exposed.

## 4. Relation predicates

Relations use deterministic bytes produced by pure transformations:

```python
v0_matches_gen0 = v0_backup_bytes == generation_zero_authority_bytes

v0_forwards_to_v1 = (
    encode_version_one(generation_zero_to_version_one(v0_backup))
    == version_one_authority_bytes
)

v1_matches_v1 = v1_snapshot_bytes == version_one_authority_bytes

v1_reverses_to_gen0 = (
    encode_generation_zero(version_one_to_v060(v1_snapshot))
    == generation_zero_authority_bytes
)
```

Semantic relations classify recovery. They never authorize overwriting an
immutable artifact; immutable equivalence remains exact protected bytes plus
the matching digest-derived name.

## 5. Issue precedence

Lower numbers win. Every lower-priority issue remains in the ordered public
issue tuple.

| Priority | Code | Meaning |
|---:|---|---|
| 10 | `unsupported_filesystem` | Parent cannot provide the approved security, lock, or durability contract |
| 20 | `unsafe_permissions` | Authority or managed object is unsafe, linked, reparse/symlinked, broadly accessible, or unassessable |
| 30 | `unreadable` | A safe relevant object cannot be bounded-read |
| 40 | `duplicate_key` | Authority or eligible prototype repeats a JSON member |
| 50 | `malformed_json` | Authority or eligible prototype is not strict UTF-8 JSON |
| 60 | `future_schema` | Authority has a strict integer schema version other than one |
| 70 | `invalid_schema` | Authority, eligible prototype, or receipt violates shape, type, or bound |
| 80 | `backup_conflict` | A managed v0/v1 artifact violates digest, generation, or canonical-content rules |
| 90 | `interrupted_artifacts` | Safe owned temporary exists or credentials remain without authority |
| 100 | `legacy_writer_detected` | Retained evidence conclusively proves a later generation-zero writer |
| 110 | `rollback_prepared` | Generation zero exactly reverses a retained v1 snapshot |
| 120 | `migration_required` | Valid generation zero needs explicit migration |
| 130 | `prototype_import_required` | No authority/recovery artifacts and an unsuppressed prototype is eligible |
| 140 | `prototype_imported` | Exact v1/prototype/receipt equality proves completed import |
| 150 | `current` | Valid version-one authority |
| 160 | `empty` | No authority, credential artifact, or unsuppressed prototype |

Within one code, issue ordering is authority, lock, v0 basenames, v1
basenames, temporary basenames, receipt basenames, then prototype. Human and
JSON output are deterministic for the same filesystem state.

Transient operation codes do not participate in passive precedence.

## 6. Authority reduction

This table assumes no higher-priority security or integrity issue.

| Authority | Additional evidence | Result |
|---|---|---|
| Absent | Any credential backup or owned temporary | `interrupted_artifacts` |
| Absent | No credential artifact | Prototype matrix |
| Generation zero | Owned temporary | `interrupted_artifacts` |
| Generation zero | At least one v1 snapshot reverses exactly | `rollback_prepared` |
| Generation zero | V1 snapshots exist but none reverse exactly | `legacy_writer_detected` |
| Generation zero | No v1 snapshot | `migration_required` |
| Version one | Owned temporary | `interrupted_artifacts` |
| Version one | Exact completed-import relation | `prototype_imported` |
| Version one | Otherwise | `current` |
| Future | Any | `future_schema` |
| Duplicate keys | Any | `duplicate_key` |
| Malformed | Any | `malformed_json` |
| Invalid | Any | `invalid_schema` |
| Unreadable | Any | `unreadable` |
| Unsafe | Any | `unsafe_permissions` |
| Unsupported filesystem | Objects are not inspected | `unsupported_filesystem` |

Valid v0 backups do not change the primary logical state:

- matching gen0 history makes migration resumable;
- a v0 backup that forwards to current v1 proves migration history;
- a differing v0 backup may simply predate legitimate v1 mutations;
- v1 without a v0 backup is valid current first-write-or-unknown-provenance
  state; and
- v0 history never becomes authority automatically.

The assessor must not claim why a backup-less v1 was created.

## 7. Prototype and receipt reduction

Prototype fallback is considered only when authority, credential backups, and
owned temporaries are absent.

Let `H(P)` be SHA-256 of exact prototype bytes and `R(H)` a valid receipt for
that digest.

| Prototype | Receipt | Result | Next command |
|---|---|---|---|
| Absent | Any or none | `empty` | None |
| Exists and exact matching receipt exists | Matching | `empty` | None |
| Valid | None | `prototype_import_required` | `sidekick-usages migrate accounts` |
| Valid | Only historical nonmatching receipt | `prototype_import_required` | Add `--reimport-prototype` |
| Duplicate keys | No matching receipt | `duplicate_key` | None |
| Malformed | No matching receipt | `malformed_json` | None |
| Invalid shape | No matching receipt | `invalid_schema` | None |
| Unreadable | No matching receipt | `unreadable` | None |
| Unsafe | No matching receipt | `unsafe_permissions` | None |

A matching receipt suppresses an unchanged already-imported prototype.
Changed prototype bytes are validated before reimport is offered.

With version-one authority, `prototype_imported` is restart-derivable only
when all three are exact:

1. a safe valid prototype;
2. a matching valid receipt; and
3. deterministic prototype-to-v1 bytes equal authoritative v1 bytes.

After a normal account mutation, the state becomes simply `current`.

## 8. Rollback and legacy writers

For valid generation-zero authority without a higher-priority issue:

| V1 snapshots | Relation | Result |
|---|---|---|
| None | Not applicable | `migration_required` |
| One or more | At least one exact reverse match | `rollback_prepared` |
| One or more | No exact reverse match | `legacy_writer_detected` |

Additional nonmatching valid snapshots are harmless history if one matches.
Invalid, unsafe, or unreadable snapshots use their higher-priority codes.

V0 backups alone cannot prove a completed v1 authority previously existed.
An active process may observe its loaded v1 baseline replaced by gen0 and
return `legacy_writer_detected`, but after restart that fact is not derivable
without retained v1 evidence.

V1 authority plus valid v1 snapshots is `current`. A retained snapshot may be
harmless history after a completed rollback and re-upgrade; it is not proof of
an interrupted operation.

## 9. Owned temporaries and missing authority

A safe exact managed temporary normally yields `interrupted_artifacts` before
the logical authority state. Passive assessment never deletes it. An explicit
coordinator may clean it only under the lock after complete reassessment.

Unsafe or unreadable temporaries use `unsafe_permissions` or `unreadable`.
Future, malformed, or invalid authority remains the primary issue when its
precedence is higher, while the temporary remains a secondary issue.

Credentials without authority are never restored automatically. Assessment
blocks writes, exposes only safe basenames, and does not prescribe a recovery
command when multiple user intentions are possible. Full reset remains an
explicit choice.

## 10. Public passive model

```python
@dataclass(frozen=True, slots=True)
class PersistenceIssue:
    code: PersistenceCode
    artifact_basename: str | None
    message: str


@dataclass(frozen=True, slots=True)
class PersistenceAssessment:
    code: PersistenceCode
    generation: StoredGeneration
    schema_version: int | None
    account_count: int | None
    safe_path: Path
    artifact_basename: str | None
    write_blocked: bool
    next_command: tuple[str, ...] | None
    message: str
    issues: tuple[PersistenceIssue, ...]
```

When issues exist, the top-level code, artifact, and message mirror the first
issue. Public data contains only Sidekick-owned codes, a safe basename, a
structured command, counts, and bounded fixed messages. It never contains raw
JSON, tokens, provider identity, validation details, OS error strings, or an
exception graph.

## 11. Passive exit mapping

| Code | Write blocked | Doctor exit | Next command |
|---|---:|---:|---|
| `empty`, `current`, `prototype_imported` | No | 0 | None |
| `migration_required` | Yes | 1 | `sidekick-usages migrate accounts` |
| `prototype_import_required` | Yes | 1 | Matrix-selected import command |
| `rollback_prepared` | Yes for current app | 0 | None |
| `legacy_writer_detected` | Yes | 1 | `sidekick-usages migrate accounts` |
| `interrupted_artifacts` | Yes | 1 | Only when recovery intent is unambiguous |
| `future_schema` | Yes | 1 | Compatible software required |
| `duplicate_key`, `malformed_json`, `invalid_schema` | Yes | 2 | None |
| `unreadable`, `unsafe_permissions`, `unsupported_filesystem` | Yes | 2 | None |
| `backup_conflict` | Yes | 2 | None |

Normal composition accepts only `empty`, `current`, and
`prototype_imported`. Doctor can render every state without constructing an
account store.

## 12. Operation results

An operation never replaces observed state with a transient code. It returns
both:

```python
@dataclass(frozen=True, slots=True)
class PersistenceOperationResult:
    code: PersistenceCode
    assessment: PersistenceAssessment
    artifact_basename: str | None
    message: str
```

| Operation code | Meaning | Exit | Restart-derived |
|---|---|---:|---:|
| `prototype_imported` | V1 and receipt committed and verified | 0 | Only through exact relation |
| `rollback_prepared` | Snapshot, gen0, and actual-v0.6 proof succeed | 0 | Relation is derivable |
| `rollback_required` | Caller requests old compatibility before preparation, or valid v1 fails pinned-reader compatibility preflight | 1 | No |
| `store_locked` | Five-second lock budget expires | 1 | Only while currently locked |
| `source_changed` | Identity or digest changed before replacement | 1 | No |
| `legacy_writer_detected` | Live baseline or snapshot relation proves old writer | 1 | Sometimes |
| `replace_failed` | Native replacement fails before confirmation | 2 | No |
| `durability_uncertain` | Replacement may have occurred but hardening/verification fails | 2 | No |
| `reset_incomplete` | Credential deletion fails or artifacts remain | 2 | No |
| `backup_conflict` | Existing immutable target differs | 2 | Yes |
| `interrupted_artifacts` | Fresh assessment observes partial artifacts | 1 | Yes |

Scheduler quiescence uses the existing scheduler exit class `3`; it is not
forced into persistence vocabulary.

## 13. Durable transition checkpoints

### Generation-zero migration

| Checkpoint | Restart state |
|---|---|
| Before backup | `migration_required` |
| Matching v0 backup published | Resumable `migration_required` |
| Output temporary exists | `interrupted_artifacts` |
| V1 replaced and verified | `current` |
| Post-replace hardening failure | Immediate `durability_uncertain`; restart classifies observed files |

### Prototype import

Authority is published before the receipt. Publishing the receipt first could
suppress the only import source while leaving no authority.

| Checkpoint | Restart state |
|---|---|
| Before V1 commit | `prototype_import_required` |
| V1 temporary exists | `interrupted_artifacts` |
| V1 committed, receipt absent | `current` |
| Receipt temporary exists | `interrupted_artifacts` |
| Receipt published and exact relation holds | `prototype_imported` |

An idempotent rerun on exact v1/prototype equality may publish the missing
receipt without rewriting v1.

### Rollback preparation

An explicit empty heartbeat target or reset collection fails pure
v0.6-compatibility preflight as operation-time `rollback_required`. Assessment
remains `current`; no snapshot, temporary, or authority mutation occurs.

| Checkpoint | Restart state |
|---|---|
| Before snapshot | `current` |
| Snapshot published, v1 still authoritative | `current` |
| Gen0 temporary exists | `interrupted_artifacts` |
| Gen0 committed and exactly reverses snapshot | `rollback_prepared` |
| v0.6 later changes gen0 | `legacy_writer_detected` |
| Re-upgrade commits v1 | `current` |

### Normal v1 persist

| Checkpoint | Restart state |
|---|---|
| Before temporary | `current` |
| Candidate temporary exists | `interrupted_artifacts` |
| Replacement verified | New `current` |
| Post-replace hardening failure | Immediate uncertainty; restart classifies observed state |

### Full reset

Credential backups and secret temporaries are deleted before authority;
authority is last. Receipt and lock remain.

| Checkpoint | Restart state |
|---|---|
| Authority remains | Existing logical state |
| Authority removed but credentials remain | `interrupted_artifacts`; immediate operation was `reset_incomplete` |
| Credential artifacts gone, matching receipt retained | `empty` |
| Prototype changed after historical receipt | `prototype_import_required --reimport-prototype` |

## 14. Facts that cannot survive restart

These are fully restart-derived:

- passive empty/current/migration/prototype/schema/integrity states;
- `backup_conflict` and `interrupted_artifacts`;
- exact `rollback_prepared` and `prototype_imported` relations; and
- snapshot-proven `legacy_writer_detected`.

These are not historical facts after restart:

- `store_locked`;
- `source_changed`;
- `replace_failed`;
- `durability_uncertain`;
- `reset_incomplete`;
- `rollback_required`;
- an ordinary v1-to-gen0 overwrite without retained v1 proof;
- whether a backup-less v1 was first write or lost history; and
- whether a v1 snapshot beside v1 represents interrupted rollback or retained
  history.

Claiming those facts later would require a new approved transaction or
provenance marker. The initial contract does not invent one.
