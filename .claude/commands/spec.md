---
description: Write the spec files for a Jira issue (reads the ticket from Jira, then runs the planner agent)
argument-hint: <JIRA-KEY e.g. SIM-20>
---

Write the specs for Jira issue **$ARGUMENTS**.

1. If `$ARGUMENTS` is empty or not a Jira key (`SIM-<n>`), stop and ask for it.
2. Read the issue with the Atlassian (Jira) MCP tools: summary, description,
   acceptance criteria, parent epic, linked issues. If it cannot be read,
   stop and say why; never invent ticket content.
3. Use the **planner** agent with the ticket text to write the spec files
   under `specs/`, named `$ARGUMENTS-<short-name>.md`, following the
   **spec-writing** skill.
4. Report: the paths written, open questions, and the risk level
   (low / needs-human-review).
5. Confluence, following the **confluence-docs** skill (each page needs my
   yes before it is written):
   - Task or Bug: draft the **Business Requirement** child page, answer the
     **Cross Functional Requirements** checklist, and propose a
     **Risk & Backlogs** row for each new risk.
   - Epic: draft the **Product Management** child page instead.
6. Ask me before writing anything to Jira. With my yes, add one comment to
   the issue listing the spec paths, the Confluence page links and open
   questions (no code, no payloads), and move it to **Spec Review** if that
   status exists.

Do not write application code in this step.
