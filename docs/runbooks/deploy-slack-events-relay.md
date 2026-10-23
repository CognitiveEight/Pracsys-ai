---
title: Deploy the Slack events relay (instant replies)
type: runbook
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "Steps"
last_verified: 2026-10-23
jira: SIM-152
risk: medium
environments: demo GCP project first, then production GCP
---

# Deploy the Slack events relay (instant replies)

## In plain words

The bot in the requests channel normally checks for new messages only about
every half hour. This small relay makes it react within about a minute: Slack
tells the relay the moment someone posts or approves a draft, and the relay
asks GitHub to run the bot straight away. The relay itself reads nothing and
stores nothing; it only says "run now". If it ever stops, the bot carries on
at its normal half-hourly pace.

## Before you start

- A GCP project for the demo, and `gcloud` installed and signed in
  (`gcloud auth login`). Your account needs, on that project: *Cloud
  Functions Developer*, *Secret Manager Admin*, *Service Account User*, and
  permission to enable APIs.
- Admin access to the Slack app **PracSys Triage** (api.slack.com/apps).
- A GitHub account with write access to `CognitiveEight/Pracsys-ai`.
- The `slack-triage.yml` workflow is on `main` (it is) and the
  `development` branch contains `relay/slack_events/`.
- Pick a region close to India: `asia-south1` (Mumbai).

Never paste a secret into chat, Slack, a file or a commit. Secrets go only into
Secret Manager, as shown below.

## Steps

Run these from the repository root.

**1. Choose the project and turn on the APIs**

```bash
export PROJECT_ID=<your-demo-project-id>
export REGION=asia-south1
gcloud config set project "$PROJECT_ID"
gcloud services enable cloudfunctions.googleapis.com run.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```
Expected: "Operation finished successfully" (or nothing, if already on).

**2. Create the GitHub token** (the relay's only power: start this workflow)

GitHub → your avatar → Settings → Developer settings → **Fine-grained tokens**
→ Generate new token:
- Resource owner: **CognitiveEight**; Repository access: **Only select
  repositories** → **Pracsys-ai**.
- Repository permissions: **Actions: Read and write**. Nothing else.
- Expiration: 90 days (put a reminder to rotate it).

If the organisation requires approval for fine-grained tokens, an org owner
approves it.

**3. Get the Slack signing secret**

api.slack.com/apps → **PracSys Triage** → Basic Information → App Credentials
→ **Signing Secret** → Show → copy.

**4. Store both secrets in Secret Manager**

Each command asks you to paste the value, then press Enter and Ctrl+D. Nothing
is echoed or saved in your shell history.

```bash
gcloud secrets create slack-signing-secret --replication-policy=automatic --data-file=-
gcloud secrets create github-dispatch-token --replication-policy=automatic --data-file=-
```
Expected: "Created version [1] of the secret".

**5. Let the function read them**

```bash
export RUNTIME_SA="$(gcloud iam service-accounts list \
  --filter='displayName:"Compute Engine default service account"' --format='value(email)')"
echo "$RUNTIME_SA"   # the function's runtime identity
for s in slack-signing-secret github-dispatch-token; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor
done
```

**6. Deploy the function**

```bash
gcloud functions deploy slack-triage-relay \
  --gen2 --region="$REGION" --runtime=python312 \
  --source=relay/slack_events --entry-point=handle \
  --trigger-http --allow-unauthenticated \
  --memory=256Mi --max-instances=3 --timeout=10s \
  --set-env-vars=GITHUB_REPO=CognitiveEight/Pracsys-ai,TRIAGE_CHANNEL_ID=C0CAXK9J7U6 \
  --set-secrets=SLACK_SIGNING_SECRET=slack-signing-secret:latest,GITHUB_DISPATCH_TOKEN=github-dispatch-token:latest
```
`--allow-unauthenticated` is required because Slack cannot send a Google
identity token; the relay authenticates every request with Slack's signature
and rejects anything else with 401.

Expected: `state: ACTIVE` and a `url:` line. Copy that URL:
```bash
gcloud functions describe slack-triage-relay --gen2 --region="$REGION" --format='value(serviceConfig.uri)'
```

**7. Point Slack at it**

api.slack.com/apps → **PracSys Triage** → **Event Subscriptions**:
1. Enable Events: **On**.
2. Request URL: paste the function URL. Slack shows **Verified ✓** within a
   few seconds (the relay answered Slack's check).
3. Subscribe to bot events → **Add Bot User Event**: `message.channels` and
   `reaction_added`.
4. **Save Changes**. If Slack asks to reinstall the app, click **Reinstall**.

## How to check it worked

1. Post a question in `#pracsys-requests`, for example "Question: which file
   types can be uploaded?".
2. Within about a minute, GitHub → Actions → **Slack triage** shows a new run
   started by **workflow_dispatch**, and the answer appears in the thread.
3. Read the relay's log (it shows only event type and outcome, never message
   text):
   ```bash
   gcloud functions logs read slack-triage-relay --gen2 --region="$REGION" --limit=20
   ```
   Expect lines like `event=message matched: workflow dispatch status=204`.
4. Repeat ten times (SIM-152 AC-10).

## If something goes wrong / how to undo

| Symptom | Likely cause | Fix |
|---|---|---|
| Slack says the Request URL did not verify | Wrong URL, or the signing secret is wrong | Check the URL from step 6; re-create the secret version with the right value and redeploy |
| Log: `rejected: bad or missing Slack signature` | Signing secret mismatch | `gcloud secrets versions add slack-signing-secret --data-file=-`, then redeploy (step 6) |
| Log: `workflow dispatch failed (HTTPError)` | Token expired, wrong repo, or missing "Actions: read and write" | Create a new token (step 2), `gcloud secrets versions add github-dispatch-token --data-file=-`, redeploy |
| Log: `ignored` for real posts | `TRIAGE_CHANNEL_ID` wrong, or the bot not in the channel | Fix the env var in step 6; `/invite @PracSys Triage` |
| Runs start but nothing is posted | The triage workflow itself (variables, secrets) | Check the run's log in GitHub Actions |

**Undo:** in Slack, Event Subscriptions → **Off**. The scheduled runs keep
working as before. To remove it entirely:
`gcloud functions delete slack-triage-relay --gen2 --region="$REGION"`.

## Who to call

- AI team: the relay and the triage workflow.
- GCP project owner: permissions, billing, APIs.
- CognitiveEight GitHub org owner: token approval.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-23 | First version, for the demo deployment | SIM-152 |
