---
description: Turn meeting notes into a Meeting & Discussion page in Confluence, with decisions and action items
argument-hint: <topic> (paste the notes after the command, or give a file path)
---

Write up the meeting: **$ARGUMENTS**.

1. Use only the notes I give you. If there are none, ask for them.
2. Follow the **confluence-docs** skill: read the **Meeting & Discussion**
   template and fill details, agenda, discussion, decisions and action items.
3. List attendees by role. Leave out client-confidential content, case
   details, personal names and phone numbers even if the notes contain them,
   and tell me what you left out.
4. Show me the title (`YYYY-MM-DD – <topic>`) and outline; create the child
   page only with my yes.
5. For each action item, offer a Jira issue in SIM; create each only when I
   confirm it.
