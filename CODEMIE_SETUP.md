# Running the catalog builder in CodeMie (MCP)

The `sn-catalog` MCP server gives a CodeMie assistant 8 guarded tools:

| Tool | What it does | Writes? |
|---|---|---|
| `get_story` | Reads the ADO story (title, description, acceptance criteria) | no |
| `sn_query` | Read-only ServiceNow lookups | no |
| `submit_build_plan` | Saves the plan, posts it as a comment on the ADO story, clears any old approval tag | ADO comment |
| `check_approval` | Checks that a listed approver added `ai-build-approved` **after** the plan was posted; creates the update set | update set |
| `sn_create_record` / `sn_update_record` | 8 catalog tables only; scope forced; blocked until approved | yes |
| `verify_build` | Checks scope, update-set capture, duplicate variables, empty choice lists | no |
| `post_ado_comment` | Posts the final summary on the approved story | ADO comment |

The approval check runs inside the server against the ADO update history, so the AI cannot approve its own plan.

---

## Step 1: Connect to the ServiceNow instance

Pick **A** for a PDI or a non-SSO dev instance, **B** for an SSO instance (e.g. the client's dev).

### A. Basic auth (PDI)
1. Create a user, e.g. `catalog_item_agent`: set a local password and tick **Web service access only**
   (also set **Identity type = Machine** if the field exists). Leave **Internal integration user** unticked.
2. Roles: `catalog_admin` + `admin` (DEV only).
3. Settings: `SN_USER=catalog_item_agent`, `SN_PASSWORD='...'`.

### B. OAuth client credentials (SSO instances; Washington DC or later)
An admin with `oauth_admin` does this on the instance:
1. **System Properties** (`sys_properties.list`): create or set
   `glide.oauth.inbound.client.credential.grant_type.enabled` = `true` (type true | false).
2. **System OAuth → Application Registry → New → Create an OAuth API endpoint for external clients**.
   Name it *CodeMie Catalog Agent* and save. Copy the **Client ID** and **Client Secret**.
3. On that record, add the **Default Grant Type** and **OAuth Application User** fields with Form Builder if they are hidden.
   Set them to **Client Credentials** and **catalog_item_agent**, with **Active** ticked.
4. Give `catalog_item_agent` the roles `catalog_admin` + `admin` (DEV only), with Web service access only ticked.
5. Settings: `SN_USER=catalog_item_agent`, `SN_CLIENT_ID=...`, `SN_CLIENT_SECRET='...'`.

### Common settings
```
SN_INSTANCE=yourclientdev          # name only, no https:// or .service-now.com
SN_ALLOWED_INSTANCES=yourclientdev # the server refuses any other instance. Never list PROD.
SN_ALLOWED_SCOPES=x_your_scope           # scopes the AI may build in (empty = any)
```

### Test the connection from your laptop first
Put the settings in `.env` and run:
```powershell
python -m pip install -r requirements.txt
python -m agent.mcp_server --check
```
Expected output: `ServiceNow auth: oauth` (or `basic`) and a sys_id for `global`.
Error meanings: **401** = credentials or OAuth setup wrong · **403** = missing roles ·
**name resolution** = wrong instance name or proxy · **OAuth token request failed** = re-check B1–B3.

The network the MCP server runs on must be able to reach the instance. If the client restricts
IP addresses, CodeMie's outbound IPs must be allowed.

## Step 2: ADO access for approvals

1. Create a PAT in the **your-ado-org** organisation with **Work Items: Read & write**.
2. Settings:
   ```
   ADO_ORG=your-ado-org
   ADO_PROJECT=your-ado-project
   ADO_PAT=...
   SN_APPROVERS=lead.name@company.com,you@company.com   # ADO sign-in emails
   APPROVAL_TAG=ai-build-approved
   ```
3. **Important:** if the CodeMie assistant also has CodeMie's own *Azure DevOps Work Item* tool, the AI could add
   the tag through that tool, using that tool's identity. Either don't give the assistant that tool
   (`get_story` covers reading), or make sure its identity is **not** in `SN_APPROVERS`.

## Step 3: Make the server installable for CodeMie

CodeMie runs MCP servers from a command (`uvx ...`). Put this project in a Git repo that CodeMie can reach
(e.g. an ADO repo) or on your internal package index. Ask the CodeMie team which they support.

## Step 4: Add the MCP server to a CodeMie assistant

1. CodeMie → **Assistants → Create assistant** (e.g. *ServiceNow Catalog Builder*).
2. **Instructions / system prompt**: paste `prompts/codemie_assistant_instructions.md`.
3. **Available tools → External tools → Manual Setup**:
   - Name: `sn-catalog`
   - Command: `uvx`
   - JSON:
     ```json
     {
       "command": "uvx",
       "args": ["--from", "git+https://<your-repo-url>@main", "sn-catalog-mcp"]
     }
     ```
     (From a package index instead: `"args": ["--from", "sn-catalog-agent", "sn-catalog-mcp"]`)
   - Set **Tools Tokens Size Limit** (CodeMie requires a value; e.g. 20000).
4. **Add environment variables** on the tool card, scoped to **User** for secrets:
   `SN_INSTANCE, SN_ALLOWED_INSTANCES, SN_ALLOWED_SCOPES, SN_USER, SN_CLIENT_ID, SN_CLIENT_SECRET`
   (or `SN_PASSWORD`), `ADO_ORG, ADO_PROJECT, ADO_PAT, SN_APPROVERS, APPROVAL_TAG`.
5. Click **Test integration**. All 8 tools should be listed.
6. Pick a strong model for the assistant (Claude if your CodeMie offers it).

## Step 5: Use it

1. In chat: *"Build the catalog item for ADO 4324 in scope x_your_scope, catalog Service Catalog, category Hardware."*
2. The assistant reads the story, looks up the instance, and posts the **plan as a comment on ADO 4324**.
3. Review the comment. To approve, an approver adds the tag **`ai-build-approved`**. For changes, reply in chat;
   the assistant submits a new plan, which needs a fresh tag.
4. Tell the assistant *"approved"*. It checks the approval, builds into update set `ADO-4324 - Catalog item (...)`,
   verifies, and posts the summary on the story.
5. Review and test the update set in DEV before promoting it as usual.

## Logs / evidence
On the machine running the server, `runs/mcp-<timestamp>-ADO-<id>/` holds `plan.json`, `audit.jsonl` (every call)
`created_records.json` and `summary.md`. Set `RUNS_DIR` to put them somewhere persistent.
The ADO comments (plan, approval tag, summary) give you an audit trail on the story for the AI scorecard.