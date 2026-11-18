"""MCP server exposing guarded ServiceNow catalog-build tools (for CodeMie, Claude Desktop, Claude Code, ...).

Run:  python -m agent.mcp_server        (stdio transport)

Approval model: writes are only allowed after a human approver (SN_APPROVERS) adds the tag
`ai-build-approved` (APPROVAL_TAG) to the ADO story *after* the plan was submitted. The check is
done here against the ADO update history, so the AI cannot approve its own plan.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .ado import AdoClient
from .servicenow import ServiceNowClient, ServiceNowError
from .tools import MAX_READ_ROWS, READ_DENY, WRITE_TABLES, RunState, verify

ROOT = Path(__file__).resolve().parent.parent
INSTRUCTIONS = """Builds ServiceNow Service Catalog items from ADO stories, safely.
Order: get_story -> sn_query (discover) -> submit_build_plan -> wait for the human to add the approval
tag in ADO -> check_approval -> sn_create_record / sn_update_record -> verify_build -> post_ado_comment.
Never invent sys_ids. Writes are blocked until check_approval returns APPROVED."""


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v


def _csv(name: str) -> list[str]:
    return [s.strip().lower() for s in (_env(name, "") or "").split(",") if s.strip()]


class Session:
    """Per-process state. CodeMie starts one stdio process per assistant session."""

    def __init__(self):
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        self.instance = _env("SN_INSTANCE")
        if not self.instance:
            raise SystemExit("SN_INSTANCE is not set")
        if self.instance.lower() not in _csv("SN_ALLOWED_INSTANCES"):
            raise SystemExit(f"SN_INSTANCE '{self.instance}' is not listed in SN_ALLOWED_INSTANCES")
        self.sn = ServiceNowClient(
            self.instance, _env("SN_USER") or "",
            password=_env("SN_PASSWORD") or None,
            client_id=_env("SN_CLIENT_ID") or None,
            client_secret=_env("SN_CLIENT_SECRET") or None,
        )
        self.ado = AdoClient(_env("ADO_ORG"), _env("ADO_PROJECT"), _env("ADO_PAT")) if _env("ADO_PAT") else None
        self.allowed_scopes = _csv("SN_ALLOWED_SCOPES")
        self.approvers = _csv("SN_APPROVERS")
        self.tag = _env("APPROVAL_TAG", "ai-build-approved")
        self.max_writes = int(_env("SN_MAX_WRITES", "150"))
        self.runs = Path(_env("RUNS_DIR") or ROOT / "runs")
        self.reset()

    def reset(self):
        self.story_id: int | None = None
        self.baseline_rev: int | None = None
        self.state: RunState | None = None
        self.approved = False

    def run_dir(self) -> Path:
        d = self.runs / f"mcp-{time.strftime('%Y%m%d-%H%M%S')}-ADO-{self.story_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d


S: Session | None = None


def _s() -> Session:
    global S
    if S is None:
        S = Session()
    return S


def _log(event: dict):
    s = _s()
    if s.state:
        s.state.log(event)



def _safe(fn):
    """Return errors as readable text instead of a generic 'Error executing tool'."""
    import functools
    import traceback

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SystemExit as e:
            return f"ERROR (configuration): {e}"
        except Exception as e:  # noqa: BLE001
            resp = getattr(e, "response", None)
            detail = ""
            if resp is not None:
                hint = {401: "ADO_PAT is invalid/expired or for another organisation",
                        403: "ADO_PAT lacks 'Work Items: Read & write' (or no permission on this project)",
                        404: "work item ID / ADO_ORG / ADO_PROJECT not found"}.get(resp.status_code, "")
                detail = f" | HTTP {resp.status_code} {hint} | {resp.text[:300]}"
            try:
                log = Path(os.getenv("RUNS_DIR") or ROOT / "runs")
                log.mkdir(parents=True, exist_ok=True)
                with open(log / "mcp-server-errors.log", "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {fn.__name__}\n{traceback.format_exc()}\n")
            except Exception:
                pass
            return f"ERROR in {fn.__name__}: {type(e).__name__}: {e}{detail}"
    return wrapper


mcp = _Server("sn-catalog", instructions=INSTRUCTIONS)


# ------------------------------------------------------------------ read tools
@mcp.tool()
@_safe
def get_story(work_item_id: int) -> str:
    """Read an Azure DevOps story (title, description, acceptance criteria) by work item ID."""
    s = _s()
    if not s.ado:
        return "ADO is not configured on this server (ADO_ORG / ADO_PROJECT / ADO_PAT)."
    return s.ado.get_story(work_item_id)


@mcp.tool()
@_safe
def sn_query(table: str, query: str = "", fields: list[str] | None = None, limit: int = 20) -> str:
    """Read-only ServiceNow Table API query. Use it to look up catalogs, categories, existing items,
    variable sets, reference tables, published flows and user criteria, and to read records back.
    `query` is an encoded query, e.g. 'nameLIKElaptop^active=true'."""
    if table in READ_DENY:
        return f"ERROR: reading {table} is not allowed."
    try:
        rows = _s().sn.query(table, query, fields, min(int(limit), MAX_READ_ROWS))
    except ServiceNowError as e:
        return f"ERROR: {e}"
    return json.dumps(rows)[:20000]


# ------------------------------------------------------------------ plan / approval
@mcp.tool()
@_safe
def submit_build_plan(work_item_id: int, scope: str, plan: dict) -> str:
    """Submit the complete build plan for the story BEFORE any write. The plan is posted as a comment on
    the ADO story for review, and any old approval tag is removed. `scope` is the application scope
    (e.g. 'x_abc_itsm' or 'global'). `plan` should contain: summary, catalog_item, variables, ui_policies,
    client_scripts, acceptance_criteria_mapping, assumptions, open_questions.
    After calling this, tell the user to review the comment in ADO and add the approval tag, then call
    check_approval. Calling it again replaces the previous plan and requires a fresh approval."""
    s = _s()
    if not s.ado:
        return "ERROR: ADO must be configured for approvals."
    if s.allowed_scopes and scope.lower() not in s.allowed_scopes:
        return f"ERROR: scope '{scope}' is not in SN_ALLOWED_SCOPES {s.allowed_scopes}."
    try:
        scope_id = s.sn.scope_sys_id(scope)
    except ServiceNowError as e:
        return f"ERROR: {e}"

    s.reset()
    s.story_id = work_item_id
    item = s.ado.get_item(work_item_id)
    tags = s.ado.tags_of(item)
    if any(t.lower() == s.tag.lower() for t in tags):
        item = s.ado.set_tags(work_item_id, [t for t in tags if t.lower() != s.tag.lower()])
    s.baseline_rev = int(item["rev"])

    stamp = time.strftime("%Y%m%d-%H%M%S")
    s.state = RunState(
        sn=s.sn, scope=scope, scope_id=scope_id, run_dir=s.run_dir(),
        update_set_name=f"ADO-{work_item_id} - Catalog item ({stamp})",
        approve_fn=lambda p: (False, "n/a"), max_writes=s.max_writes,
    )
    (s.state.run_dir / "plan.json").write_text(json.dumps({"scope": scope, **plan}, indent=2), encoding="utf-8")
    _log({"tool": "submit_build_plan", "story": work_item_id, "scope": scope, "baseline_rev": s.baseline_rev})

    s.ado.add_comment(work_item_id, _plan_markdown(scope, plan, s.tag))
    who = ", ".join(s.approvers) if s.approvers else "an approver"
    return (f"PENDING_APPROVAL: plan posted as a comment on ADO #{work_item_id}. Ask {who} to review it and add "
            f"the tag '{s.tag}' to the story, then call check_approval. To change the plan, call submit_build_plan again.")


def _plan_markdown(scope: str, plan: dict, tag: str) -> str:
    lines = [f"AI BUILD PLAN (scope: {scope})", "", plan.get("summary", ""), ""]
    ci = plan.get("catalog_item", {})
    lines.append("Catalog item: " + (json.dumps(ci) if not isinstance(ci, str) else ci))
    lines.append("")
    lines.append("Variables:")
    for v in plan.get("variables", []):
        if isinstance(v, dict):
            lines.append(f"- {v.get('order', '')} {v.get('name', '')} [{v.get('type', '')}]"
                         f"{' (mandatory)' if v.get('mandatory') else ''}: {v.get('question_text', '')}")
        else:
            lines.append(f"- {v}")
    for key in ("ui_policies", "client_scripts", "acceptance_criteria_mapping", "assumptions", "open_questions"):
        items = plan.get(key) or []
        if items:
            lines += ["", key.replace("_", " ").title() + ":"]
            lines += [f"- {json.dumps(i) if isinstance(i, dict) else i}" for i in items]
    lines += ["", f"To approve: add the tag '{tag}' to this work item."]
    return "\n".join(lines)


@mcp.tool()
@_safe
def check_approval() -> str:
    """Check whether a listed approver has added the approval tag to the ADO story since the plan was
    submitted. Returns APPROVED (and prepares the update set) or PENDING."""
    s = _s()
    if not s.state or s.story_id is None:
        return "ERROR: submit_build_plan first."
    if s.approved:
        return f"APPROVED (update set: {s.state.update_set_name})"

    approver = _find_approval(s)
    if not approver:
        return (f"PENDING: no approval yet. Waiting for "
                f"{', '.join(s.approvers) if s.approvers else 'an approver'} to add '{s.tag}' to ADO #{s.story_id}.")
    try:
        s.state.update_set_id = s.sn.set_context(
            s.state.scope_id, s.state.update_set_name, f"Approved in ADO by {approver}")
    except ServiceNowError as e:
        return f"ERROR preparing update set: {e}"
    s.approved = s.state.approved = True
    _log({"tool": "check_approval", "approved_by": approver, "update_set": s.state.update_set_id})
    return f"APPROVED by {approver}. Update set '{s.state.update_set_name}' is current. You may now build."


def _find_approval(s: Session) -> str | None:
    item = s.ado.get_item(s.story_id)
    if not any(t.lower() == s.tag.lower() for t in s.ado.tags_of(item)):
        return None
    for u in reversed(s.ado.updates(s.story_id)):
        if int(u.get("rev", 0)) <= s.baseline_rev:
            break
        tags = (u.get("fields") or {}).get("System.Tags")
        if not tags:
            continue
        new = (tags.get("newValue") or "").lower()
        old = (tags.get("oldValue") or "").lower()
        if s.tag.lower() in new and s.tag.lower() not in old:
            by = u.get("revisedBy") or {}
            who = (by.get("uniqueName") or "").lower()
            name = by.get("displayName") or who
            if not s.approvers or who in s.approvers:
                return name
            return None  # tag added by someone not allowed to approve
    return None


# ------------------------------------------------------------------ write tools
def _guard_write(table: str) -> str | None:
    s = _s()
    if not s.approved or not s.state:
        return "ERROR: blocked — plan not approved. Call check_approval."
    if table not in WRITE_TABLES:
        return f"ERROR: blocked — {table} is not in the write allow-list {sorted(WRITE_TABLES)}."
    if s.state.writes >= s.state.max_writes:
        return "ERROR: blocked — write limit for this run reached."
    return None


@mcp.tool()
@_safe
def sn_create_record(table: str, fields: dict) -> str:
    """Create a record in an allow-listed catalog table (sc_cat_item, item_option_new, question_choice,
    io_set_item, catalog_ui_policy, catalog_ui_policy_action, catalog_script_client,
    sc_cat_item_user_criteria_mtom). Only after approval. The scope is set automatically. Returns sys_id."""
    if err := _guard_write(table):
        return err
    st = _s().state
    data = {k: v for k, v in fields.items() if k not in ("sys_id", "sys_scope")}
    data["sys_scope"] = st.scope_id
    try:
        rec = st.sn.create(table, data)
    except ServiceNowError as e:
        _log({"tool": "sn_create_record", "table": table, "error": str(e)})
        return f"ERROR: {e}"
    st.writes += 1
    entry = {"table": table, "sys_id": rec.get("sys_id"),
             "name": rec.get("name") or rec.get("short_description") or rec.get("text")}
    st.created.append(entry)
    (st.run_dir / "created_records.json").write_text(json.dumps(st.created, indent=2), encoding="utf-8")
    _log({"tool": "sn_create_record", "input": fields, **entry})
    return json.dumps(entry)


@mcp.tool()
@_safe
def sn_update_record(table: str, sys_id: str, fields: dict) -> str:
    """Update a record in an allow-listed catalog table. Only records inside the approved scope."""
    if err := _guard_write(table):
        return err
    st = _s().state
    data = {k: v for k, v in fields.items() if k not in ("sys_id", "sys_scope")}
    try:
        if st.sn.get(table, sys_id, ["sys_scope"]).get("sys_scope") != st.scope_id:
            return f"ERROR: blocked — {table}/{sys_id} is not in scope {st.scope}."
        st.sn.update(table, sys_id, data)
    except ServiceNowError as e:
        return f"ERROR: {e}"
    st.writes += 1
    _log({"tool": "sn_update_record", "table": table, "sys_id": sys_id, "input": fields})
    return json.dumps({"table": table, "sys_id": sys_id, "updated": list(data)})


@mcp.tool()
@_safe
def verify_build(cat_item_sys_id: str) -> str:
    """Check the built item: records are in scope and captured in the update set, variable names are
    unique, choice lists are populated."""
    s = _s()
    if not s.state:
        return "ERROR: nothing built in this session."
    try:
        return json.dumps(verify(s.state, cat_item_sys_id))
    except ServiceNowError as e:
        return f"ERROR: {e}"


@mcp.tool()
@_safe
def post_ado_comment(text: str) -> str:
    """Post the final build summary as a comment on the approved ADO story."""
    s = _s()
    if not (s.ado and s.story_id and s.approved):
        return "ERROR: only allowed for the approved story in this session."
    s.ado.add_comment(s.story_id, text)
    (s.state.run_dir / "summary.md").write_text(text, encoding="utf-8")
    return "posted"


def main():
    # stdout is the MCP channel — never print to it.
    if "--check" in sys.argv:  # quick connectivity test from a terminal
        s = _s()
        print(f"ServiceNow auth: {s.sn.auth_mode}", file=sys.stderr)
        print("sys_scope global ->", s.sn.scope_sys_id("global"), file=sys.stderr)
        if s.ado:
            print("ADO reachable for", os.getenv("ADO_ORG"), "/", os.getenv("ADO_PROJECT"), file=sys.stderr)
        return
    mcp.run()


if __name__ == "__main__":
    main()
