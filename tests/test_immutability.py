"""已签发结论不可变：服务层与数据库层双重保证。"""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from steel_audit.errors import ConflictError  # noqa: E402
from support import add_sample, dt, make_line, make_measured_rule, make_service  # noqa: E402


class ImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        make_line(self.svc)
        make_measured_rule(self.svc)
        add_sample(self.svc, "L1", "uid-1", "100",
                   "2026-06-10T00:00", "2026-06-10T12:00")
        draft = self.svc.compute("L1", "month", "2026-06-01T00:00")
        self.issued = self.svc.issue(draft["conclusion_id"])

    def test_issued_conclusion_cannot_be_updated_at_db_level(self):
        # 绕过服务层直接写库，触发器必须拦截
        with self.assertRaises(sqlite3.IntegrityError):
            self.svc.store._conn.execute(
                "UPDATE conclusions SET totals='{}' WHERE conclusion_id=?",
                (self.issued["conclusion_id"],))

    def test_issued_conclusion_cannot_be_deleted_at_db_level(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.svc.store._conn.execute(
                "DELETE FROM conclusions WHERE conclusion_id=?",
                (self.issued["conclusion_id"],))

    def test_issued_conclusion_cannot_be_reissued(self):
        with self.assertRaises(ConflictError):
            self.svc.issue(self.issued["conclusion_id"])

    def test_issued_content_stable_after_new_data(self):
        """签发后即使新样本送达，原结论内容保持原样。"""
        before = self.svc.get_conclusion(self.issued["conclusion_id"])
        self.clock.set(dt("2026-07-05T00:00"))
        add_sample(self.svc, "L1", "uid-2", "999",
                   "2026-06-20T00:00", "2026-06-20T12:00",
                   received_at="2026-07-05T00:00")
        after = self.svc.get_conclusion(self.issued["conclusion_id"])
        self.assertEqual(before["totals"], after["totals"])
        self.assertEqual(before["detail"], after["detail"])
        self.assertEqual(after["totals"]["total_emission"], "100.000000")

    def test_draft_can_be_recomputed_freely(self):
        """草稿不进入更正链，可反复重算；只有签发版构成脉络。"""
        draft = self.svc.compute("L1", "month", "2026-06-01T00:00")
        self.assertEqual(draft["status"], "draft")
        lineage = self.svc.get_lineage(draft["conclusion_id"])
        # 草稿不在任何已签发链上，链上只有它自己
        self.assertEqual(len(lineage["chain"]), 1)


if __name__ == "__main__":
    unittest.main()
