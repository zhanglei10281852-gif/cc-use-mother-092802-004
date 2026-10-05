"""样本接入的确定处理规则：幂等、冲突、重复、撤回。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from steel_audit.errors import ConflictError  # noqa: E402
from support import add_sample, dt, make_line, make_measured_rule, make_service  # noqa: E402


class SampleIngestTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        make_line(self.svc)
        make_measured_rule(self.svc)

    def test_idempotent_replay_same_uid_same_content(self):
        first = add_sample(self.svc, "L1", "uid-1", "10",
                           "2026-06-01T00:00", "2026-06-01T04:00")
        replay = add_sample(self.svc, "L1", "uid-1", "10",
                            "2026-06-01T00:00", "2026-06-01T04:00")
        self.assertEqual(first["outcome"], "accepted")
        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(first["sample"]["sample_id"], replay["sample"]["sample_id"])
        self.assertEqual(len(self.svc.list_samples("L1")), 1)

    def test_same_uid_different_content_conflicts(self):
        add_sample(self.svc, "L1", "uid-1", "10",
                   "2026-06-01T00:00", "2026-06-01T04:00")
        with self.assertRaises(ConflictError):
            add_sample(self.svc, "L1", "uid-1", "99",
                       "2026-06-01T00:00", "2026-06-01T04:00")

    def test_duplicate_natural_key_first_received_wins(self):
        first = add_sample(self.svc, "L1", "uid-1", "10",
                           "2026-06-01T00:00", "2026-06-01T04:00")
        dup = add_sample(self.svc, "L1", "uid-2", "20",
                         "2026-06-01T00:00", "2026-06-01T04:00")
        self.assertEqual(dup["outcome"], "duplicate_flagged")
        self.assertEqual(dup["sample"]["status"], "duplicate")
        self.assertEqual(dup["sample"]["duplicate_of"], first["sample"]["sample_id"])

        # 核算只采信先到样本；重复件出现在证据缺口中
        conclusion = self.svc.compute("L1", "day", "2026-06-01T00:00")
        self.assertEqual(conclusion["totals"]["total_emission"], "10.000000")
        gap_types = {g["type"] for g in conclusion["gaps"]}
        self.assertIn("duplicate_samples_excluded", gap_types)

    def test_withdraw_rules(self):
        accepted = add_sample(self.svc, "L1", "uid-1", "10",
                              "2026-06-01T00:00", "2026-06-01T04:00")
        sid = accepted["sample"]["sample_id"]
        withdrawn = self.svc.withdraw_sample(sid, reason="仪器校准异常")
        self.assertEqual(withdrawn["outcome"], "withdrawn")
        self.assertEqual(withdrawn["sample"]["status"], "withdrawn")
        # 重复撤回：幂等返回，不报错
        again = self.svc.withdraw_sample(sid)
        self.assertEqual(again["outcome"], "already_withdrawn")

    def test_withdraw_duplicate_is_rejected(self):
        add_sample(self.svc, "L1", "uid-1", "10",
                   "2026-06-01T00:00", "2026-06-01T04:00")
        dup = add_sample(self.svc, "L1", "uid-2", "20",
                         "2026-06-01T00:00", "2026-06-01T04:00")
        with self.assertRaises(ConflictError):
            self.svc.withdraw_sample(dup["sample"]["sample_id"])

    def test_no_auto_promotion_after_withdrawal(self):
        """原样本撤回后，其重复件不会自动生效；需新编号重报。"""
        first = add_sample(self.svc, "L1", "uid-1", "10",
                           "2026-06-01T00:00", "2026-06-01T04:00")
        add_sample(self.svc, "L1", "uid-2", "20",
                   "2026-06-01T00:00", "2026-06-01T04:00")
        self.svc.withdraw_sample(first["sample"]["sample_id"])

        # 重算：两件都被排除，总量为 0
        conclusion = self.svc.compute("L1", "day", "2026-06-01T00:00")
        self.assertEqual(conclusion["totals"]["total_emission"], "0.000000")

        # 新编号重报同一自然键：被接受并计入
        resubmitted = add_sample(self.svc, "L1", "uid-3", "30",
                                 "2026-06-01T00:00", "2026-06-01T04:00")
        self.assertEqual(resubmitted["outcome"], "accepted")
        conclusion = self.svc.compute("L1", "day", "2026-06-01T00:00")
        self.assertEqual(conclusion["totals"]["total_emission"], "30.000000")

    def test_invalid_sample_rejected(self):
        with self.assertRaises(Exception):
            add_sample(self.svc, "L1", "uid-9", "abc",
                       "2026-06-01T00:00", "2026-06-01T04:00")
        with self.assertRaises(Exception):
            add_sample(self.svc, "L1", "uid-9", "10",
                       "2026-06-01T04:00", "2026-06-01T00:00")  # 结束早于开始


if __name__ == "__main__":
    unittest.main()
