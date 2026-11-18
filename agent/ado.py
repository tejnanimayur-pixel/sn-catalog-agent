"""Azure DevOps work-item reader / commenter."""
from __future__ import annotations

import base64
import html
import re

import requests


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<\s*(br|/p|/div|/li)\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


class AdoClient:
    def __init__(self, org: str, project: str, pat: str, timeout: int = 30):
        self.base = f"https://dev.azure.com/{org}/{project}/_apis"
        token = base64.b64encode(f":{pat}".encode()).decode()
        self.h = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self.timeout = timeout

    def get_story(self, work_item_id: int) -> str:
        r = requests.get(
            f"{self.base}/wit/workitems/{work_item_id}",
            params={"$expand": "all", "api-version": "7.1"},
            headers=self.h, timeout=self.timeout,
        )
        r.raise_for_status()
        f = r.json()["fields"]
        parts = [
            f"ADO #{work_item_id}: {f.get('System.Title', '')}",
            f"Type: {f.get('System.WorkItemType', '')} | State: {f.get('System.State', '')}",
            "",
            "## Description",
            _strip_html(f.get("System.Description")),
            "",
            "## Acceptance Criteria",
            _strip_html(f.get("Microsoft.VSTS.Common.AcceptanceCriteria")),
        ]
        return "\n".join(parts)

    def title(self, work_item_id: int) -> str:
        r = requests.get(
            f"{self.base}/wit/workitems/{work_item_id}",
            params={"fields": "System.Title", "api-version": "7.1"},
            headers=self.h, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["fields"]["System.Title"]

    def add_comment(self, work_item_id: int, markdown_text: str):
        # ADO comments render HTML; wrap as preformatted-ish paragraphs.
        body = "<br>".join(html.escape(line) for line in markdown_text.splitlines())
        r = requests.post(
            f"{self.base}/wit/workItems/{work_item_id}/comments",
            params={"api-version": "7.1-preview.4"},
            json={"text": body}, headers=self.h, timeout=self.timeout,
        )
        r.raise_for_status()

    # ---- approval support (used by the MCP server) ------------------------
    def get_item(self, work_item_id: int) -> dict:
        r = requests.get(
            f"{self.base}/wit/workitems/{work_item_id}",
            params={"fields": "System.Title,System.Tags", "api-version": "7.1"},
            headers=self.h, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()  # includes "rev" and "fields"

    @staticmethod
    def tags_of(item: dict) -> list[str]:
        raw = item.get("fields", {}).get("System.Tags") or ""
        return [t.strip() for t in raw.split(";") if t.strip()]

    def set_tags(self, work_item_id: int, tags: list[str]) -> dict:
        r = requests.patch(
            f"{self.base}/wit/workitems/{work_item_id}",
            params={"api-version": "7.1"},
            json=[{"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)}],
            headers={**self.h, "Content-Type": "application/json-patch+json"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def updates(self, work_item_id: int) -> list[dict]:
        out, skip = [], 0
        while True:
            r = requests.get(
                f"{self.base}/wit/workItems/{work_item_id}/updates",
                params={"$top": 200, "$skip": skip, "api-version": "7.1"},
                headers=self.h, timeout=self.timeout,
            )
            r.raise_for_status()
            page = r.json().get("value", [])
            out += page
            if len(page) < 200:
                return out
            skip += 200
