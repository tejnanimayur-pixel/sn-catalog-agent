"""Tool schemas for Claude + a guarded dispatcher."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .servicenow import ServiceNowClient, ServiceNowError

# Tables the agent may WRITE to. Everything else is read-only.
WRITE_TABLES = {
    "sc_cat_item",
    "item_option_new",
    "question_choice",
    "io_set_item",
    "catalog_ui_policy",
    "catalog_ui_policy_action",
    "catalog_script_client",
    "sc_cat_item_user_criteria_mtom",
}
# Tables the agent may not even read (credentials / session data).
READ_DENY = {"sys_user_preference", "sys_auth_profile_basic", "oauth_entity", "sys_certificate",
             "discovery_credentials", "sys_user_token"}
MAX_READ_ROWS = 50


TOOLS = [
    {
        "name": "sn_query",
        "description": (
            "Read-only query against the ServiceNow Table API. Use to look up catalogs, categories, "
            "existing items, variable sets, reference tables, flows, user criteria, and to read back records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "query": {"type": "string", "description": "Encoded query, e.g. 'nameLIKElaptop^active=true'"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["table"],
        },
    },
    {
        "name": "submit_build_plan",
        "description": (
            "Submit the full build plan for human approval. Must be called before any write. "
            "Returns APPROVED, or REJECTED with reviewer feedback to address."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "catalog_item": {
                    "type": "object",
                    "description": "name, short_description, description, catalog (name + sys_id), "
                                   "category (name + sys_id), fulfilment flow (name + sys_id or null)",
                },
                "variables": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Each: order, name, question_text, type code + type name, mandatory, "
                                   "choices / reference table / default, help_text",
                },
                "variable_sets_reused": {"type": "array", "items": {"type": "object"}},
                "ui_policies": {"type": "array", "items": {"type": "object"},
                                "description": "Each: condition in words, actions (variable -> visible/mandatory/read-only)"},
                "client_scripts": {"type": "array", "items": {"type": "object"},
                                   "description": "Each: name, type, variable, purpose (why a UI policy is not enough)"},
                "acceptance_criteria_mapping": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "catalog_item", "variables", "acceptance_criteria_mapping"],
        },
    },
    {
        "name": "sn_create_record",
        "description": (
            "Create a record in an allow-listed catalog table (only after plan approval). "
            f"Allowed tables: {', '.join(sorted(WRITE_TABLES))}. Returns sys_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "enum": sorted(WRITE_TABLES)},
                "fields": {"type": "object"},
            },
            "required": ["table", "fields"],
        },
    },
    {
        "name": "sn_update_record",
        "description": "Update a record in an allow-listed table. Only records in the target scope can be changed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "enum": sorted(WRITE_TABLES)},
                "sys_id": {"type": "string"},
                "fields": {"type": "object"},
            },
            "required": ["table", "sys_id", "fields"],
        },
    },
    {
        "name": "verify_build",
        "description": (
            "Check the built catalog item: every record is in the target scope and captured in the "
            "run's update set, variables have unique names, choice lists are populated. Returns a report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"cat_item_sys_id": {"type": "string"}},
            "required": ["cat_item_sys_id"],
        },
    },
]

ADO_COMMENT_TOOL = {
    "name": "post_ado_comment",
    "description": "Post the final build summary (markdown) as a comment on the source ADO work item.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


@dataclass
class RunState:
    sn: ServiceNowClient
    scope: str
    scope_id: str
    run_dir: Path
    update_set_name: str
    approve_fn: Callable[[dict], tuple[bool, str]]
    dry_run: bool = False
    max_writes: int = 150
    ado_comment_fn: Callable[[str], None] | None = None
    approved: bool = False
    update_set_id: str | None = None
    writes: int = 0
    created: list[dict] = field(default_factory=list)

    def log(self, event: dict):
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(self.run_dir / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")


def _ok(data) -> tuple[str, bool]:
    return json.dumps(data, default=str)[:20000], False


def _err(msg: str) -> tuple[str, bool]:
    return msg, True


def dispatch(state: RunState, name: str, args: dict) -> tuple[str, bool]:
    """Run one tool call. Returns (content, is_error)."""
    state.log({"tool": name, "input": args})
    try:
        out = _dispatch(state, name, args)
    except ServiceNowError as e:
        out = _err(str(e))
    except Exception as e:  # surface to the model rather than crash the run
        out = _err(f"{type(e).__name__}: {e}")
    state.log({"tool": name, "is_error": out[1], "output": out[0][:2000]})
    return out


def _dispatch(state: RunState, name: str, args: dict) -> tuple[str, bool]:
    sn = state.sn

    if name == "sn_query":
        table = args["table"]
        if table in READ_DENY:
            return _err(f"Reading {table} is not allowed.")
        limit = min(int(args.get("limit", 20)), MAX_READ_ROWS)
        return _ok(sn.query(table, args.get("query", ""), args.get("fields"), limit))

    if name == "submit_build_plan":
        (state.run_dir / "plan.json").write_text(json.dumps(args, indent=2), encoding="utf-8")
        if state.dry_run:
            return _ok({"status": "DRY_RUN", "message": "Plan saved. Do not build. Give the final summary now."})
        approved, feedback = state.approve_fn(args)
        if not approved:
            return _ok({"status": "REJECTED", "feedback": feedback})
        state.approved = True
        state.update_set_id = sn.set_context(
            state.scope_id, state.update_set_name, args.get("summary", "")[:1000]
        )
        return _ok({"status": "APPROVED", "feedback": feedback,
                    "update_set": state.update_set_name, "update_set_sys_id": state.update_set_id})

    if name in ("sn_create_record", "sn_update_record"):
        if not state.approved:
            return _err("Blocked: submit_build_plan must be approved before any write.")
        table = args["table"]
        if table not in WRITE_TABLES:
            return _err(f"Blocked: {table} is not in the write allow-list.")
        if state.writes >= state.max_writes:
            return _err("Blocked: write limit for this run reached.")
        fields = dict(args["fields"])
        fields.pop("sys_id", None)
        fields.pop("sys_scope", None)  # scope is controlled by the harness, not the model

        if name == "sn_create_record":
            fields["sys_scope"] = state.scope_id
            rec = sn.create(table, fields)
            state.writes += 1
            entry = {"table": table, "sys_id": rec.get("sys_id"),
                     "name": rec.get("name") or rec.get("short_description") or rec.get("text")}
            state.created.append(entry)
            return _ok(entry)

        existing = sn.get(table, args["sys_id"], ["sys_scope"])
        if existing.get("sys_scope") != state.scope_id:
            return _err(f"Blocked: {table}/{args['sys_id']} is not in scope {state.scope}.")
        rec = sn.update(table, args["sys_id"], fields)
        state.writes += 1
        return _ok({"table": table, "sys_id": rec.get("sys_id"), "updated": list(fields)})

    if name == "verify_build":
        return _ok(verify(state, args["cat_item_sys_id"]))

    if name == "post_ado_comment":
        if not state.ado_comment_fn:
            return _err("ADO commenting is not enabled for this run.")
        state.ado_comment_fn(args["text"])
        return _ok({"status": "posted"})

    return _err(f"Unknown tool {name}")


def verify(state: RunState, item_id: str) -> dict:
    sn = state.sn
    issues: list[str] = []
    item = sn.get("sc_cat_item", item_id, ["name", "sys_scope", "active", "sc_catalogs", "category"])
    if item.get("sys_scope") != state.scope_id:
        issues.append("Catalog item is not in the target scope.")
    if not item.get("sc_catalogs"):
        issues.append("Catalog item is not assigned to a catalog.")

    vars_ = sn.query("item_option_new", f"cat_item={item_id}",
                     ["sys_id", "name", "type", "sys_scope", "order"], 200)
    names = [v["name"] for v in vars_]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        issues.append(f"Duplicate variable names: {sorted(dupes)}")
    for v in vars_:
        if v.get("sys_scope") != state.scope_id:
            issues.append(f"Variable {v['name']} is not in the target scope.")
        if v.get("type") in ("3", "5"):
            n = sn.query("question_choice", f"question={v['sys_id']}", ["sys_id"], 1)
            if not n:
                issues.append(f"Choice variable {v['name']} has no choices.")

    captured = 0
    if state.update_set_id:
        captured = len(sn.query("sys_update_xml", f"update_set={state.update_set_id}", ["sys_id"], 500))
        if captured < len(state.created):
            issues.append(
                f"Only {captured} updates captured in update set for {len(state.created)} records created — "
                "check the integration user's current update set / application."
            )

    return {
        "item": item.get("name"),
        "variables": len(vars_),
        "records_created_this_run": len(state.created),
        "update_set_captured": captured,
        "issues": issues or ["none"],
    }
