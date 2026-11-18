"""Offline tests for the MCP server: tool listing over stdio + approval logic with a fake ADO/SN."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.update({"SN_INSTANCE": "devx", "SN_ALLOWED_INSTANCES": "devx", "SN_USER": "u", "SN_PASSWORD": "p",
                   "RUNS_DIR": tempfile.mkdtemp(), "SN_APPROVERS": "lead@corp.com"})

from agent import mcp_server as M  # noqa: E402


class FakeSN:
    auth_mode = "basic"

    def __init__(self):
        self.created = []

    def scope_sys_id(self, scope):
        return "scope1"

    def set_context(self, scope_id, name, desc):
        return "us1"

    def create(self, table, data):
        self.created.append((table, data))
        return {"sys_id": f"r{len(self.created)}", **data}

    def query(self, *a, **k):
        return []


class FakeADO:
    def __init__(self):
        self.rev, self.tags, self.updates_list, self.comments = 5, ["ai-build-approved"], [], []

    def get_item(self, i):
        return {"rev": self.rev, "fields": {"System.Tags": "; ".join(self.tags)}}

    tags_of = staticmethod(M.AdoClient.tags_of)

    def set_tags(self, i, tags):
        self.rev += 1
        self.tags = tags
        return self.get_item(i)

    def add_comment(self, i, text):
        self.comments.append(text)

    def updates(self, i):
        return self.updates_list

    def human_tags(self, who):
        old = "; ".join(self.tags)
        self.rev += 1
        self.tags = self.tags + ["ai-build-approved"]
        self.updates_list.append({"rev": self.rev, "revisedBy": {"uniqueName": who, "displayName": who},
                                  "fields": {"System.Tags": {"oldValue": old, "newValue": "; ".join(self.tags)}}})


def fresh():
    M.S = None
    s = M._s()
    s.sn, s.ado = FakeSN(), FakeADO()
    return s


PLAN = {"summary": "Laptop", "catalog_item": {"name": "Laptop"}, "variables": [{"name": "model", "type": 5}]}


class Approval(unittest.TestCase):
    def test_full_flow(self):
        s = fresh()
        self.assertIn("ERROR", M.sn_create_record("sc_cat_item", {"name": "x"}))
        out = M.submit_build_plan(4324, "global", PLAN)
        self.assertIn("PENDING_APPROVAL", out)
        self.assertNotIn("ai-build-approved", s.ado.tags)           # stale tag removed
        self.assertEqual(len(s.ado.comments), 1)                     # plan posted to ADO
        self.assertIn("PENDING", M.check_approval())
        self.assertIn("ERROR", M.sn_create_record("sc_cat_item", {"name": "x"}))

        s.ado.human_tags("dev@corp.com")                             # not an approver
        self.assertIn("PENDING", M.check_approval())

        s.ado.tags.remove("ai-build-approved")
        s.ado.human_tags("lead@corp.com")                            # approver
        self.assertIn("APPROVED", M.check_approval())
        out = json.loads(M.sn_create_record("sc_cat_item", {"name": "Laptop", "sys_scope": "global"}))
        self.assertEqual(s.sn.created[0][1]["sys_scope"], "scope1")  # scope forced
        self.assertIn("ERROR", M.sn_create_record("sys_script", {}))  # allow-list
        self.assertEqual(out["sys_id"], "r1")

    def test_resubmit_needs_new_approval(self):
        s = fresh()
        M.submit_build_plan(4324, "global", PLAN)
        s.ado.human_tags("lead@corp.com")
        self.assertIn("APPROVED", M.check_approval())
        M.submit_build_plan(4324, "global", PLAN)                    # plan changed
        self.assertIn("PENDING", M.check_approval())
        self.assertIn("ERROR", M.sn_create_record("sc_cat_item", {"name": "x"}))

    def test_scope_allowlist(self):
        fresh().allowed_scopes = ["x_abc_itsm"]
        self.assertIn("not in SN_ALLOWED_SCOPES", M.submit_build_plan(1, "global", PLAN))


class StdioListing(unittest.TestCase):
    def test_tools_listed_over_stdio(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            params = StdioServerParameters(command=sys.executable, args=["-m", "agent.mcp_server"],
                                           cwd=str(ROOT), env=dict(os.environ))
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as sess:
                    await sess.initialize()
                    return sorted(t.name for t in (await sess.list_tools()).tools)

        names = asyncio.run(run())
        self.assertEqual(names, sorted(["get_story", "sn_query", "submit_build_plan", "check_approval",
                                        "sn_create_record", "sn_update_record", "verify_build", "post_ado_comment"]))


if __name__ == "__main__":
    unittest.main()
