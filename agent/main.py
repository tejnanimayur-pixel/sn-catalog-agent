"""CLI entry point: python -m agent.main --ado 12345 --scope x_acme_itsm"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic

from .ado import AdoClient
from .servicenow import ServiceNowClient
from .tools import ADO_COMMENT_TOOL, TOOLS, RunState, dispatch

ROOT = Path(__file__).resolve().parent.parent
MAX_TURNS = 60


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        sys.exit(f"Missing environment variable {name} (see .env.example)")
    return v


def check_instance_allowed(instance: str):
    allowed = [s.strip().lower() for s in os.getenv("SN_ALLOWED_INSTANCES", "").split(",") if s.strip()]
    if not allowed:
        sys.exit("SN_ALLOWED_INSTANCES is empty — list your DEV instance(s) explicitly.")
    if instance.lower() not in allowed:
        sys.exit(f"Instance '{instance}' is not in SN_ALLOWED_INSTANCES {allowed}. Refusing to run.")


def render_plan(plan: dict) -> str:
    out = [f"\n=== BUILD PLAN ===\n{plan.get('summary', '')}\n"]
    ci = plan.get("catalog_item", {})
    out.append("Catalog item: " + json.dumps(ci, indent=2))
    out.append("\nVariables:")
    for v in plan.get("variables", []):
        out.append(f"  {v.get('order', ''):>5}  {v.get('name', ''):<30} {v.get('type', '')}  "
                   f"{'*' if v.get('mandatory') else ' '}  {v.get('question_text', '')}")
    for key in ("variable_sets_reused", "ui_policies", "client_scripts",
                "acceptance_criteria_mapping", "assumptions", "open_questions"):
        items = plan.get(key) or []
        if items:
            out.append(f"\n{key.replace('_', ' ').title()}:")
            out += [f"  - {json.dumps(i) if isinstance(i, dict) else i}" for i in items]
    return "\n".join(out)


def console_approval(plan: dict) -> tuple[bool, str]:
    print(render_plan(plan))
    ans = input("\nApprove and build? [y = yes / anything else = feedback to revise]: ").strip()
    if ans.lower() in ("y", "yes"):
        return True, "Approved by reviewer."
    return False, ans or "Rejected without comment — re-check the story and resubmit."


def main():
    # Windows consoles default to cp1252; avoid crashes on arrows, emoji etc.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    load_env()
    p = argparse.ArgumentParser(description="Build a ServiceNow catalog item from a story with Claude.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--ado", type=int, help="ADO work item ID")
    src.add_argument("--story-file", help="Path to a text/markdown file with the story")
    src.add_argument("--story", help="Story text inline")
    p.add_argument("--scope", required=True, help="Application scope, e.g. x_acme_itsm (or 'global')")
    p.add_argument("--catalog", default="Service Catalog", help="Target catalog name")
    p.add_argument("--category", default="", help="Target category name (optional)")
    p.add_argument("--dry-run", action="store_true", help="Plan only; no writes")
    p.add_argument("--comment-ado", action="store_true", help="Post the summary back on the ADO item")
    p.add_argument("--model", default=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
    a = p.parse_args()

    instance = need("SN_INSTANCE")
    check_instance_allowed(instance)
    sn = ServiceNowClient(
        instance, need("SN_USER"),
        password=os.getenv("SN_PASSWORD") or None,
        client_id=os.getenv("SN_CLIENT_ID") or None,
        client_secret=os.getenv("SN_CLIENT_SECRET") or None,
    )
    print(f"ServiceNow auth mode: {sn.auth_mode}")

    ado = None
    if a.ado or a.comment_ado:
        ado = AdoClient(need("ADO_ORG"), need("ADO_PROJECT"), need("ADO_PAT"))

    if a.ado:
        story = ado.get_story(a.ado)
        story_ref = f"ADO-{a.ado}"
    elif a.story_file:
        story = Path(a.story_file).read_text(encoding="utf-8")
        story_ref = Path(a.story_file).stem
    else:
        story = a.story
        story_ref = "inline"

    scope_id = sn.scope_sys_id(a.scope)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "runs" / f"{stamp}-{re.sub(r'[^A-Za-z0-9_-]', '_', story_ref)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "story.md").write_text(story, encoding="utf-8")

    state = RunState(
        sn=sn, scope=a.scope, scope_id=scope_id, run_dir=run_dir,
        update_set_name=f"{story_ref} - Catalog item ({stamp})",
        approve_fn=console_approval, dry_run=a.dry_run,
        ado_comment_fn=(lambda t: ado.add_comment(a.ado, t)) if (a.comment_ado and a.ado) else None,
    )

    tools = TOOLS + ([ADO_COMMENT_TOOL] if state.ado_comment_fn else [])
    system_prompt = (ROOT / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
    user_msg = (
        f"Target instance: {instance}\nTarget scope: {a.scope} (sys_id {scope_id})\n"
        f"Target catalog: {a.catalog}\n"
        + (f"Target category: {a.category}\n" if a.category else "")
        + ("MODE: DRY RUN — plan only, no writes.\n" if a.dry_run else "")
        + f"\n<story>\n{story}\n</story>\n\nBuild the catalog item described in this story."
    )

    ws = os.getenv("ANTHROPIC_WORKSPACE_ID")  # only needed for organization-level keys
    client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws} if ws else None)
    messages = [{"role": "user", "content": user_msg}]
    usage = {"input": 0, "output": 0}
    started = time.time()
    final_text = ""

    for turn in range(MAX_TURNS):
        resp = client.messages.create(
            model=a.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
        )
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})

        for block in resp.content:
            if block.type == "text" and block.text.strip():
                print(f"\n[Claude] {block.text}")
                final_text = block.text

        if resp.stop_reason != "tool_use":
            break

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            print(f"  -> {block.name} {json.dumps(block.input)[:160]}")
            content, is_error = dispatch(state, block.name, block.input)
            if is_error:
                print(f"     ! {content[:300]}")
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    else:
        print("Stopped: turn limit reached.")

    (run_dir / "summary.md").write_text(final_text, encoding="utf-8")
    (run_dir / "created_records.json").write_text(json.dumps(state.created, indent=2), encoding="utf-8")

    # Metrics for the AI scorecard: one row per run.
    metrics = ROOT / "runs" / "metrics.csv"
    new = not metrics.exists()
    with open(metrics, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "story", "scope", "dry_run", "approved", "records_created",
                        "duration_min", "input_tokens", "output_tokens", "model"])
        w.writerow([stamp, story_ref, a.scope, a.dry_run, state.approved, len(state.created),
                    round((time.time() - started) / 60, 1), usage["input"], usage["output"], a.model])

    print(f"\nRun folder: {run_dir}")


if __name__ == "__main__":
    main()
