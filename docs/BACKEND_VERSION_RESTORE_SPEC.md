# Backend Implementation Spec — Version Restore & Document Deletion

For the backend team. Covers everything the backend must implement for the AI
service to handle **version restore** and to report **deleted documents as
missing information**.

The AI service side is already implemented and merged on `development`.

**TL;DR** — there is exactly **one new event to emit** (`workspace_version_restore`)
and **one hard rule** (`evidenceFiles` must always be the *full* surviving set,
and every file must carry `_id`). Everything else below is either already in
place or a recommendation.

---

## 1. The one new event: `workspace_version_restore`

Emit this when a user restores a workspace to an earlier document version.
Send it to the **same room / channel you already use for `workspace_sync`** —
same socket, same auth, no transport change.

### Payload

```jsonc
{
  "workspaceId": "665f…",            // required
  "userId": "664a…",                 // required
  "versionId": "v_20260730_1042",    // required — see §1.2
  "updatedAt": "2026-07-30T10:42:11Z", // required, ISO 8601

  // REQUIRED and AUTHORITATIVE — the FULL surviving document set at the
  // restored version. Not a delta. See §1.1.
  "evidenceFiles": [ /* EvidenceFile objects — see §2 */ ],

  // Advisory cross-checks. Nice to have, not relied on.
  "deletedDocumentIds":  ["665f…invoice"],
  "restoredDocumentIds": ["665f…ledger"],

  // Optional, all behave exactly as on workspace_sync
  "workspaceRole": "PETITIONER",     // PETITIONER | RESPONDENT | MEDIATOR | ADJUDICATOR
  "title": "Sharma vs Acme Retail",
  "status": "active",
  "currentState": { /* see §3 — strongly recommended */ },
  "structure": "Bullet Points",
  "tone": "Formal",
  "description": "Use Indian legal terms only"
}
```

Custom instructions may alternatively be sent nested, exactly as on
`workspace_sync`: `"customInstructions": { "structure": …, "tone": …, "description": … }`.

### 1.1 `evidenceFiles` must be the FULL surviving set — this is the critical rule

The AI does **not** trust `deletedDocumentIds` / `restoredDocumentIds`. It reads
the documents currently indexed in its own vector store, diffs them against
`evidenceFiles`, and reconciles to `evidenceFiles`:

```
to_delete = (currently indexed)  −  (evidenceFiles)
to_ingest = (evidenceFiles)      −  (currently indexed)
```

Consequences you need to be aware of:

- **A document you omit from `evidenceFiles` gets deleted from the AI's index**,
  whether or not you listed it in `deletedDocumentIds`. Sending a partial list
  will silently destroy evidence.
- Conversely, a stale document you forgot to mark deleted is cleaned up
  automatically. The design is self-correcting *as long as the list is complete*.
- Sending the two advisory arrays wrong is harmless. Sending `evidenceFiles`
  incomplete is not.

### 1.2 `versionId` — required, and it must change per restore target

`versionId` keys the AI's replay guard. The AI suppresses a repeat of the *same*
`workspaceId` + `versionId` + `updatedAt` + advisory arrays within **180
seconds**, and processes anything else.

- Use your real version/revision identifier for the version being restored **to**.
- Must be **stable** for the same restore (so a socket-level retry is correctly
  deduped) and **different** for a different restore target.
- Do **not** send a fresh UUID on every emit — that defeats the guard and you'll
  pay for duplicate LLM regeneration.
- Restoring A → B → A is fine: the second A carries a new `updatedAt`, so it is
  not treated as a replay.

### 1.3 The restored files must actually be downloadable

When a restore brings a document back, the AI **re-downloads and re-processes it
from scratch** (its vectors were deleted). So at the moment you emit:

- `url` must be a **live, valid pre-signed URL** with enough TTL to survive
  download + OCR. This is the single most likely cause of a failed restore.
- The underlying object must still exist in storage. If your delete is a hard
  delete of the GCS object, restore cannot work — deletes must be **soft** at the
  storage layer for versioning to mean anything.

Re-ingestion is billed (OCR, transcription, embeddings, per-document LLM calls) —
see §5.

---

## 2. `EvidenceFile` object — every field is required

This is the existing shape used by `workspace_created` / `workspace_sync`. It is
strictly validated: **if any required field is missing, the entire event is
rejected** and nothing regenerates.

| Field | Type | Required | Notes |
|---|---|---|---|
| `_id` | string | **effectively yes** | Schema-optional, but a file without it **cannot be reconciled and is skipped** (logged as a warning). Always send it. |
| `originalName` | string | yes | |
| `storeFileName` | string | yes | Stored filename with UUID |
| `filename` | string | yes | Display filename with annexure prefix |
| `extension` | string | yes | |
| `fileHash` | string | yes | SHA-256 |
| `url` | string | yes | Pre-signed URL — must be live, see §1.3 |
| `path` | string | yes | Storage path |
| `uploadedAt` | string | yes | ISO date |
| `isDeleted` | boolean | no | Defaults `false`. Entries with `true` are treated as not present. |

`_id` must be **stable across delete and restore** — it is the identity the AI
matches on. If a restore assigns a new `_id` to the same document, the AI sees a
delete plus an unrelated upload: it will re-ingest (re-billing) and the original
will be reported as permanently missing evidence.

---

## 3. `currentState` — send it, on both `workspace_sync` and `workspace_version_restore`

Optional, but without it every regenerated item is emitted as **new** with a
freshly minted id, so the user's curation (approved/pending, and the ids your
frontend holds) is lost on every restore.

```jsonc
"currentState": {
  "missingInfo":          [ { "id": "missingInfo_ab12cd34ef56", "text": "…", "status": "approved" } ],
  "risks":                [ { "id": "…", "text": "…", "status": "pending" } ],
  "weaknesses":           [ { "id": "…", "text": "…" } ],
  "relevantJurisdiction": [ { "id": "…", "text": "…" } ],
  "relevantParties":      { "approved": [ { "id": "…", "name": "…" } ], "pending": [] },
  "listOfDates":          { "approved": [ { "id": "…", "date": "…", "title": "…" } ], "pending": [] },
  "applicableLaws":       [ { "act": "BNS", "section": "318" } ]
}
```

Every sub-block is optional; omit a block when that panel is empty. Unknown keys
are ignored. Matching rules: text items by normalised text, parties by name,
dates by date + title, laws by act + section. Only omit `currentState` entirely
if you genuinely have no curated state — an omitted field never clobbers the last
state the AI cached, but an omitted field also can't preserve ids.

---

## 4. What does NOT change

- **No outbound schema change.** Everything the AI emits back keeps its existing
  shape. `analysis_report.missingInfo` is still `[{ id, text, isMatched }]` — it
  will simply now *contain* entries for deleted documents. No new fields, no new
  `featureType`.
- **`workspace_created` unchanged.**
- **`workspace_sync` unchanged.** Keep sending it exactly as today for ordinary
  uploads and deletes. Its existing delete signals all still work:
  `deletedDocumentIds`, per-file `isDeleted: true`, or simply omitting the file
  from `evidenceFiles`.
- **You do not have to implement the new event to get the deleted-document fix.**
  The missing-information behaviour is driven by the deletion signal on
  `workspace_sync`, which you already send. `workspace_version_restore` adds
  explicit, correctly-deduped, guaranteed-full regeneration on top. **Ship the
  restore event second if you want to split the work.**

---

## 5. What the AI emits back (so you can wire storage + billing)

On a restore the AI emits, in this order:

1. `workspace:refresh` — `reason: "ai_update_started"`, with the `featureTypes`
   whose loaders should turn on. `document_assessments` is included **only** when
   documents are actually being re-ingested.
2. `document_feature_response` with `featureType: "document_assessments"` — only
   when something was re-ingested. **Covers only the restored documents**, not the
   whole workspace; upsert by `documentId` rather than replacing your list.
3. `document_feature_response` with `featureType: "workspace_metadata"` — sent
   with `workspaceTitle: ""` and `updateTitle: false`, i.e. **a restore never
   renames the workspace**. Do not overwrite the title from this payload.
4. `strategy_objective`, then `analysis_report`.
5. Then, in the background: `list_of_dates`, `applicable_laws`,
   `procedural_timeline`, `relevant_parties`, `case_laws`, `drafting_assistant`.

Cost: each emit carries the standard `metadata` block. A restore that re-ingests
documents produces **real, non-zero `price`** on the `document_assessments` emit
(OCR units, STT seconds, embeddings, per-document LLM calls) plus normal
per-feature cost on the rest. `price`/`totalPrice` already include paid non-LLM
services. Budget for a restore costing roughly the same as the original upload of
the restored documents.

**Deleted-document behaviour to expect in the UI:** a deleted document's content
is fully purged — it will never be cited in strengths, weaknesses or annexure
references again — but its *type* now appears as a `missingInfo` gap (e.g. a
deleted invoice yields "The invoice … has been removed from the evidence set …").
That gap disappears automatically once the document is restored or re-uploaded.
Because `missingInfo` is non-empty, `caseResolutionScore` is capped at 95.

---

## 6. Recommended: stop hard-deleting, and keep `_id` stable

Two things that aren't strictly required but will bite you otherwise:

1. **Soft-delete at the storage layer.** Restore re-downloads from `url`. A hard
   delete makes the version unrestorable.
2. **`_id` stability** across delete → restore (see §2).

## 7. Known limitation worth a ticket on your side

The AI decides whether to re-ingest purely by "is this `_id` already indexed" —
it currently **ignores `fileHash` for evidence files**. So if you ever replace a
document's *content* while keeping the same `_id`, the AI will not notice and will
keep serving the old text. Either mint a new `_id` on content replacement, or ask
us to wire `fileHash` into the evidence-ingestion path (the template-sync path
already does this correctly). Not a blocker for version restore.

---

## 8. Acceptance checklist

Run against a dev/staging AI service and watch its log.

| # | Action | Expected |
|---|---|---|
| 1 | `workspace_created` with 3 docs incl. an invoice | `document_assessments` lists 3 |
| 2 | `workspace_sync` adding a 4th doc | No previously-uploaded doc appears in `missingInfo` |
| 3 | `workspace_sync` with `deletedDocumentIds: [invoice]` | `analysis_report.missingInfo` contains an invoice entry; no strength/weakness cites the invoice; score ≤ 95 |
| 4 | `workspace_version_restore` back to the 3-doc version, `evidenceFiles` = all 3 | Invoice entry gone from `missingInfo`; `document_assessments` re-emitted for the invoice; `metadata.price` > 0 |
| 5 | Re-emit the identical restore payload twice within 180 s | AI logs "Skipping duplicate/replayed workspace_version_restore"; no duplicate emits, no double billing |
| 6 | Restore with an `evidenceFiles` entry missing `_id` | AI logs "skipped N evidence file(s) with no _id"; that file is neither ingested nor deleted |
| 7 | Restore with an expired `url` | Re-ingestion fails and is logged; other panels still regenerate; assessments loader is cleared, not left spinning |

For 5, reuse the byte-identical payload including `updatedAt` — the guard hashes it.

---

## 9. Questions to send back to us

- Do you want the AI to emit an explicit terminal ack for a restore (e.g. a
  `workspace_version_restore_response`)? Today completion is implied by the final
  panel emits.
- Should a restore preserve or discard the user's rejected-item memory (gaps they
  dismissed)? Currently preserved, except that a gap created by a *deletion* can
  always resurface — dismissing a gap because a document answered it must not
  hide it permanently once that document is withdrawn.
