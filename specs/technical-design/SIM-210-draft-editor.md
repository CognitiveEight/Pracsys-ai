# SIM-210: Draft editor — technical design (backend and frontend)

- **Requirements:** `specs/requirements/SIM-210-draft-editor.md`
- **Status:** draft
- **Approvers:** backend lead, frontend lead

## Summary
A self-hosted ONLYOFFICE Docs server renders and edits the .docx in the
browser. The frontend embeds it; the backend owns the file and the
permissions. The AI service is unchanged: it produces the .docx (SIM-202,
SIM-203, SIM-208) and the backend treats it as version 1 of the draft.

```
AI service ──draftDocument.downloadUrl──► Backend (stores v1)
Frontend ──"Open editor"──► Backend: signed editor config ──► Frontend ──► ONLYOFFICE editor
ONLYOFFICE ──save callback (status 2/6, url)──► Backend: download the edited .docx, store vN+1
```

## Components
| Part | Change | Owner |
|---|---|---|
| ONLYOFFICE Docs | Docker image `onlyoffice/documentserver`, JWT enabled (`JWT_ENABLED=true`, `JWT_SECRET` from the secret store), HTTPS, reachable from the browser and the backend | DevOps |
| Backend (Node) | `GET /drafts/:id/editor-config`; `POST /drafts/:id/onlyoffice-callback`; versioned draft storage | Backend |
| Frontend | the ONLYOFFICE editor React component (`@onlyoffice/document-editor-react`) on "Open editor" | Frontend |
| AI service | none | — |

## Editor config (built and signed by the backend)
```json
{
  "documentType": "word",
  "document": {
    "fileType": "docx",
    "key": "<draftId>-v<version>",
    "title": "<draft title>.docx",
    "url": "<short-lived signed URL of the current version>",
    "permissions": {"edit": true, "review": true, "comment": true, "download": true}
  },
  "editorConfig": {
    "mode": "edit",
    "callbackUrl": "https://<backend>/drafts/<id>/onlyoffice-callback",
    "user": {"id": "<userId>", "name": "<display name>"},
    "customization": {"review": {"trackChanges": true}, "forcesave": true}
  },
  "token": "<JWT of this object, signed with JWT_SECRET>"
}
```
- **`document.key`** must change whenever the file changes (a new version),
  and stay the same while people co-edit the same version.
- **Permissions:**
  - `edit` collaborator: `mode: "edit"`;
  - `view` collaborator: `mode: "view"` with `edit`/`review`/`comment`
    false. This mirrors the `mutations_allowed` rule in the AI service.
- **Track changes on by default** (`trackChanges: true`): every edit is a
  suggestion that can be accepted or rejected, which is the review workflow
  lawyers expect. The owner can switch it off in the editor.

## Save callback
ONLYOFFICE POSTs JSON `{key, status, url, users, ...}`, signed with the
same JWT. The backend must:
1. Verify the JWT (in the body `token`, or the `Authorization` header);
   otherwise reply 403.
2. On `status` **2** (closed with changes, ready to save) or **6** (force
   save): download `url` and store it as the next version, with author and
   time.
3. On `status` **3** or **7** (save error): log the failure and alert.
4. Reply `{"error": 0}`. Anything else makes the document server retry.

## Download and AI
- Download serves the latest stored version.
- Tracked changes and comments are part of the .docx (`w:ins`/`w:del`,
  `comments.xml`), so Microsoft Word shows them.
- Re-generating a draft with the AI creates a new version. The editor
  config then gets a new `key`.

## Security and privacy
- The document URL is short-lived and signed; the callback is JWT-verified.
- The document server has no other access to storage.
- Drafts never leave our infrastructure.
- The document server's own cache is purged when a workspace is deleted.
  The backend must call the cache-clean API (or delete by `key`) in its
  workspace-deletion flow.

## Risks
- **Concurrency limit** of the Community Edition: measure, and plan an
  upgrade path.
- **Memory and CPU** of the document server: size it from load tests.
- **Font fidelity:** install Times New Roman / Arial-metric fonts on the
  document server, or the layout shifts.

## Test plan
- **Backend:**
  - config signing and permissions (edit vs view);
  - callback JWT verification;
  - status 2 and 6 store a version; status 3 and 7 alert; the reply is
    always `{"error":0}`.
- **Frontend:** the editor opens; view mode is read-only.
- **Manual:** track a change, add a comment, close, download, open in
  Microsoft Word and see both.
