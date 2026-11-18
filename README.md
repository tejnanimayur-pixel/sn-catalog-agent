# ServiceNow Catalog Builder Agent (Claude)

Turns a user story — an ADO work item number or pasted text — into a working Service Catalog item
on a ServiceNow **DEV** instance, inside the application scope you choose, captured in its own update set.

```
ADO story / prompt ──► Claude reads story ──► looks up instance (catalog, category, variable sets,
                                              reference tables, flows) ──► BUILD PLAN
                                                                             │ you approve / give feedback
                                                                             ▼
          summary + ADO comment ◄── verify ◄── builds: item → variables → choices → variable sets
                                                      → UI policies → client scripts → user criteria
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # fill in keys
```

### ServiceNow integration user (DEV only)
- Create a dedicated user, e.g. `svc_claude_catalog`, with **web service access only**.
- Roles: `catalog_admin` + `admin` (or `delegated_developer` on the target scope if your
  governance allows — it must be able to create records in the scope and set its own preferences).
- Put only DEV/sandbox instance names in `SN_ALLOWED_INSTANCES`. The agent refuses any other instance.

### ADO
A Personal Access Token with **Work Items: Read & write** (write is only needed for `--comment-ado`).

## Run

```bash
# Plan only — nothing is written. Good first run.
python -m agent.main --ado 12345 --scope x_acme_itsm --dry-run

# Build from ADO, then post the summary back on the story
python -m agent.main --ado 12345 --scope x_acme_itsm --catalog "Service Catalog" --category "Hardware" --comment-ado

# Build from a pasted story
python -m agent.main --story-file tests/sample_story.md --scope x_acme_itsm
```

When the plan appears, type `y` to build, or type feedback (e.g. *"use the existing Requested For
variable set; make delivery location optional"*). Claude then revises the plan and asks again.

## Safety guardrails (enforced in code, not just the prompt)

| Guardrail | Where |
|---|---|
| No writes until a human approves the plan | `tools.py` → `_dispatch` |
| Writes only to 8 catalog tables (no business rules, script includes, ACLs, deletes) | `WRITE_TABLES` |
| `sys_scope` forced to the target scope on every create; updates blocked outside the scope | `_dispatch` |
| All changes go into a new update set (`ADO-12345 - Catalog item (...)`) | `servicenow.py` → `set_context` |
| Instance allow-list; credential tables can't be read; max 150 writes per run | `main.py`, `READ_DENY` |
| Full audit log of every tool call | `runs/<run>/audit.jsonl` |

Run the offline tests: `python -m unittest tests.test_guardrails -v`

## What each run leaves behind (`runs/<timestamp>-ADO-12345/`)

- `story.md` — the story as read
- `plan.json` — the approved build plan (attach to the ADO story as design evidence)
- `created_records.json` — every record created, with sys_ids
- `audit.jsonl` — every tool call and result
- `summary.md` — final report: what was built, acceptance criteria mapping, open questions
- `runs/metrics.csv` — one row per run: duration, records created, tokens. **Use this as evidence
  for the AI scorecard** (compare against your historical effort per catalog item).

## How scope and update set capture work

Before building, the agent creates an update set in your scope and sets two preferences on the
integration user: `apps.current_app` (scope) and `sys_update_set`. The client doesn't keep session
cookies, so every API call starts a fresh session that picks up these preferences.
`verify_build` then checks that the records were captured in `sys_update_xml`. If it reports a
mismatch, open the update set and compare it to `created_records.json`.

## Known limits / next steps

- **Fulfilment flows are not generated.** The agent links an existing published flow, or lists it as a
  manual follow-up. Flow Designer content is best built by hand or with Now Assist for Creator.
- **Multi-row variable sets, catalog client script includes, and Service Portal widgets** are out of scope.
- **Automate the trigger:** add an ADO service hook (work item tagged `ai-catalog`) → a small webhook/pipeline
  that runs `--dry-run` and posts the plan as a comment; a developer approves and runs the build.
- **Add ATF:** extend the tool list with `sys_atf_test` / `sys_atf_step` to generate tests from the
  acceptance criteria. This is a natural second use case for the scorecard.
- Always review the update set before promoting it. You remain accountable for what goes to TEST/PROD.
