---
name: confluence-docs
description: The SimplerToday Confluence SDLC templates (Business Requirement, Product Management, Cross Functional Requirements, Risk & Backlogs, Release Management, Retrospective, Meeting & Discussion, Developer Onboarding, Project Management) - which page each workflow step must fill and how. Use with every /spec, /implement, /review, /release-check, /retro and /meeting run, and whenever a task needs a Confluence page drafted or updated.
---

# Confluence SDLC templates

Every task in the agentic workflow leaves its record in Confluence, on a child
page of the matching template. The repository stays the source of truth for
code and specs; Confluence holds the business, project and release record.

## Where the templates live

Atlassian site: the SimplerToday site. Resolve its cloud id with the Atlassian
MCP tool that lists accessible resources; do not hard-code a URL.
Space id: `1802242`. Root page: **SimplerToday – SDLC Templates** (`1867777`).

| Template page | Page id | Child page title |
|---|---|---|
| Business Requirement | `2129921` | `SIM-<n> – <feature name>` |
| Product Management | `1998849` | `SIM-<epic n> – <initiative>` |
| Developer Onboarding Process | `2031617` | (edited in place; no children) |
| Meeting & Discussion | `2064385` | `YYYY-MM-DD – <topic>` |
| Project Management | `2097153` | (status table edited in place) |
| ↳ Risk & Backlogs | `2162689` | (risk rows added in place) |
| ↳ Cross Functional Requirements | `2195457` | `SIM-<n> – NFR check` |
| ↳ Release Management | `2228225` | `vX.Y.Z – <target> – YYYY-MM-DD` |
| ↳ Retrospective | `2260993` | `YYYY-MM-DD – Retro – <scope>` |

## Which step fills which page

| Workflow step | Confluence work |
|---|---|
| `/spec` on a Task or Bug | Draft a **Business Requirement** child page from the Jira issue. Answer the **Cross Functional Requirements** checklist (in the technical design, or as an `NFR check` child page). Propose a **Risk & Backlogs** row for every new risk the design carries. |
| `/spec` on an Epic | Draft a **Product Management** child page from the epic and its children. |
| `/implement` | If setup changed (Makefile, requirements, `env.example` variable names, docker-compose, `.claude/`), propose an update to **Developer Onboarding Process**. |
| `/review` | Propose a **Risk & Backlogs** row for each risk the PR leaves open. |
| `/release-check` | Draft a **Release Management** child page from the commit range, Jira keys and CHANGELOG. |
| `/retro` | Draft a **Retrospective** child page: facts from Jira and git only. |
| `/meeting` | Turn supplied notes into a **Meeting & Discussion** child page. |
| Weekly status (on request) | Refresh the epic table on **Project Management** from Jira. |

## How to write a page

1. Read the template page first (`getConfluencePage`, markdown) and copy its
   sections in order. Do not invent new sections or drop existing ones.
2. Fill only what you can source from Jira, the repository or the user's
   input. Everything else stays as the template hint, or goes under
   **Open questions**.
3. Before any create or update, show the user the title, the parent page and
   a short outline, and **wait for a yes**. One confirmation covers one page.
4. Create it as a child of the template page (`parentId` = the template's
   page id, `spaceId` = `1802242`, `contentFormat` = `markdown`). For
   in-place pages, read the current version first and change only the part
   your step owns.
5. Link both ways: put the page link in a Jira comment (after confirmation)
   and the Jira key in the page's overview table.
6. Report the page title and link back to the user.

Markdown note: write placeholders as `SIM-…`, not `SIM-___`; underscores
render as bold or italic in Confluence.

## Never

- Write case data, client, party or participant names, phone numbers,
  document text, objective text, secrets, `.env` values or signed URLs.
  People are named by role.
- Tick a checklist box you did not verify, fill a sign-off, set
  likelihood/impact/owner on a risk, or mark anything approved. Those are
  human decisions.
- Delete or move Confluence pages.
- Let Confluence override the repository: if a page and `CLAUDE.md`
  disagree, `CLAUDE.md` wins; tell the user so the page can be corrected.
