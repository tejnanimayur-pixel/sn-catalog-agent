"""Offline tests for the tool guardrails (no ServiceNow / Claude calls). Run: python -m unittest tests.test_guardrails"""
import json
import tempfile
import unittest
from pathlib import Path

from agent.tools import RunState, dispatch

SCOPE_ID = "scope123"


class FakeSN:
    def __init__(self):
        self.records = {}
        self.n = 0

    def query(self, table, query="", fields=None, limit=20):
        return []

    def get(self, table, sys_id, fields=None):
        return self.records.get(sys_id, {"sys_scope": "global"})

    def create(self, table, data):
        self.n += 1
        sid = f"id{self.n}"
        self.records[sid] = {**data, "sys_id": sid}
        return self.records[sid]

    def update(self, table, sys_id, data):
        self.records[sys_id].update(data)
        return self.records[sys_id]

    def set_context(self, scope_id, name, desc):
        return "us1"


def make_state(approve=True, dry_run=False):
    return RunState(sn=FakeSN(), scope="x_test", scope_id=SCOPE_ID, run_dir=Path(tempfile.mkdtemp()),
                    update_set_name="t", approve_fn=lambda p: (approve, "fb"), dry_run=dry_run)


PLAN = {"summary": "s", "catalog_item": {}, "variables": [], "acceptance_criteria_mapping": []}


class Guardrails(unittest.TestCase):
    def test_write_blocked_before_plan(self):
        s = make_state()
        out, err = dispatch(s, "sn_create_record", {"table": "sc_cat_item", "fields": {"name": "x"}})
        self.assertTrue(err)
        self.assertIn("approved", out)

    def test_rejected_plan_keeps_writes_blocked(self):
        s = make_state(approve=False)
        out, _ = dispatch(s, "submit_build_plan", PLAN)
        self.assertEqual(json.loads(out)["status"], "REJECTED")
        _, err = dispatch(s, "sn_create_record", {"table": "sc_cat_item", "fields": {}})
        self.assertTrue(err)

    def test_dry_run_never_approves(self):
        s = make_state(dry_run=True)
        out, _ = dispatch(s, "submit_build_plan", PLAN)
        self.assertEqual(json.loads(out)["status"], "DRY_RUN")
        self.assertFalse(s.approved)

    def test_create_forces_scope_and_allowlist(self):
        s = make_state()
        dispatch(s, "submit_build_plan", PLAN)
        out, err = dispatch(s, "sn_create_record",
                            {"table": "sc_cat_item", "fields": {"name": "Laptop", "sys_scope": "global"}})
        self.assertFalse(err)
        sid = json.loads(out)["sys_id"]
        self.assertEqual(s.sn.records[sid]["sys_scope"], SCOPE_ID)
        _, err = dispatch(s, "sn_create_record", {"table": "sys_script", "fields": {}})
        self.assertTrue(err)

    def test_update_outside_scope_blocked(self):
        s = make_state()
        dispatch(s, "submit_build_plan", PLAN)
        out, err = dispatch(s, "sn_update_record",
                            {"table": "sc_cat_item", "sys_id": "someone_elses", "fields": {"active": "false"}})
        self.assertTrue(err)
        self.assertIn("not in scope", out)

    def test_sensitive_read_blocked(self):
        s = make_state()
        _, err = dispatch(s, "sn_query", {"table": "sys_user_preference"})
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
