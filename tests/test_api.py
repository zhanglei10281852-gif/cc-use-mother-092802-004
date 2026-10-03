"""端到端 HTTP API 测试（标准库 http.client，无需第三方框架）。"""

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from steel_audit.api import create_server
from steel_audit.service import AuditService


class ApiClient:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port

    def call(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        data = json.loads(raw) if raw else None
        conn.close()
        return resp.status, data

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path, body)


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.service = AuditService()
        self.server = create_server("127.0.0.1", 0, service=self.service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.api = ApiClient("127.0.0.1", self.port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _seed_line_and_rule(self, limit="10", rule_version="1.0"):
        status, _ = self.api.post("/lines", {
            "plant_id": "P1", "line_id": "L1", "capacity_tonnes": "100",
            "commissioned_at": "2026-01-01T00:00:00Z",
        })
        self.assertEqual(status, 200)
        status, _ = self.api.post("/rules", {
            "rule_id": "R", "version": rule_version,
            "effective_at": "2026-01-01T00:00:00Z",
            "limits": {"dust": limit},
            "canonical_units": {"dust": "mg/m3"},
            "min_samples": 1, "sampling_interval_hours": "4",
        })
        self.assertEqual(status, 200)


class RegistrationApiTests(ApiTestBase):
    def test_idempotent_submission_and_conflict(self):
        self._seed_line_and_rule()
        sample = {
            "sample_id": "s1", "line_id": "L1", "pollutant": "dust",
            "measured_at": "2026-03-01T02:00:00Z", "value": "8",
            "unit": "mg/m3", "received_at": "2026-03-01T03:00:00Z",
        }
        s1, _ = self.api.post("/samples", sample)
        s2, _ = self.api.post("/samples", sample)
        self.assertEqual((s1, s2), (200, 200))
        bad = dict(sample, value="9")
        status, err = self.api.post("/samples", bad)
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "conflict")

    def test_bad_timezone_rejected(self):
        self._seed_line_and_rule()
        status, err = self.api.post("/samples", {
            "sample_id": "s1", "line_id": "L1", "pollutant": "dust",
            "measured_at": "2026-03-01T02:00:00", "value": "8",
            "unit": "mg/m3",
        })
        self.assertEqual(status, 400)

    def test_unknown_route_and_missing_entity(self):
        status, _ = self.api.get("/nope")
        self.assertEqual(status, 404)
        status, err = self.api.get("/conclusions/ghost")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"], "not_found")


class LineSwitchApiTests(ApiTestBase):
    def test_line_switch_marks_phase_gap_and_splits_evidence(self):
        self._seed_line_and_rule()
        for eq in [
            {"equipment_id": "OLD", "line_id": "L1",
             "valid_from": "2026-02-01T00:00:00Z",
             "valid_to": "2026-03-01T12:00:00Z", "phase": "baseline"},
            {"equipment_id": "NEW", "line_id": "L1",
             "valid_from": "2026-03-01T12:00:00Z", "phase": "retrofit"},
        ]:
            status, _ = self.api.post("/equipment", eq)
            self.assertEqual(status, 200)
        for sid, hour, value in [("w1", 10, "16"), ("w2", 12, "4"),
                                 ("w3", 14, "4")]:
            h0 = f"{hour:02d}:00:00Z"
            h1 = f"{hour + 2:02d}:00:00Z"
            status, _ = self.api.post("/samples", {
                "sample_id": sid, "line_id": "L1", "pollutant": "dust",
                "measured_at": f"2026-03-01T{h0}", "value": value,
                "unit": "mg/m3", "received_at": "2026-03-01T20:00:00Z",
                "covers_from": f"2026-03-01T{h0}",
                "covers_to": f"2026-03-01T{h1}",
            })
            self.assertEqual(status, 200)

        status, detail = self.api.post("/recalculate", {
            "line_id": "L1", "period": "day",
            "anchor": "2026-03-01T12:00:00Z", "rule_id": "R",
            "as_of": "2026-03-02T00:00:00Z",
        })
        self.assertEqual(status, 200)
        codes = {g["code"] for g in detail["gaps"]}
        self.assertIn("phase_switch_in_period", codes)
        phases = {s["phase"] for s in detail["segments"]}
        self.assertEqual(phases, {"baseline", "retrofit"})
        # 监管视角：明细里每一段都能追到设备
        for seg in detail["segments"]:
            self.assertIn(seg["equipment_id"], {"OLD", "NEW"})


class WithdrawalAndCorrectionApiTests(ApiTestBase):
    def _issue_first_conclusion(self):
        self._seed_line_and_rule()
        self.api.post("/equipment", {
            "equipment_id": "E1", "line_id": "L1",
            "valid_from": "2026-02-01T00:00:00Z", "phase": "baseline",
        })
        for i, hour in enumerate(range(0, 24, 4)):
            self.api.post("/samples", {
                "sample_id": f"s{i}", "line_id": "L1", "pollutant": "dust",
                "measured_at": f"2026-03-01T{hour:02d}:00:00Z",
                "value": "9", "unit": "mg/m3",
                "received_at": f"2026-03-02T{hour:02d}:00:00Z",
            })
        status, c1 = self.api.post("/conclusions", {
            "line_id": "L1", "period": "day",
            "anchor": "2026-03-01T12:00:00Z", "rule_id": "R",
            "issued_by": "regulator-a", "as_of": "2026-03-03T00:00:00Z",
            "conclusion_id": "C1",
        })
        self.assertEqual(status, 201)
        return c1

    def test_withdraw_then_correct_lineage_and_detail_query(self):
        c1 = self._issue_first_conclusion()
        self.assertEqual(c1["verdict"], "compliant")

        status, _ = self.api.post("/samples/s2/withdraw", {
            "reason": "监测机构复核：采样探头故障",
            "at": "2026-03-04T00:00:00Z",
        })
        self.assertEqual(status, 200)
        status, _ = self.api.post("/samples", {
            "sample_id": "s2-fix", "line_id": "L1", "pollutant": "dust",
            "measured_at": "2026-03-01T08:00:00Z", "value": "5",
            "unit": "mg/m3", "received_at": "2026-03-04T06:00:00Z",
        })
        self.assertEqual(status, 200)

        status, c2 = self.api.post("/conclusions/C1/corrections", {
            "reason": "样本 s2 撤回，替换为 s2-fix",
            "issued_by": "regulator-b", "as_of": "2026-03-05T00:00:00Z",
            "conclusion_id": "C2",
        })
        self.assertEqual(status, 201)
        self.assertEqual(c2["superseding_ids"], ["C1"])
        self.assertNotEqual(c2["calc_hash"], c1["calc_hash"])

        # 旧结论不可变：GET 回来内容仍为原样，仅状态变为 superseded
        status, old = self.api.get("/conclusions/C1")
        self.assertEqual(status, 200)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["calc_hash"], c1["calc_hash"])
        self.assertEqual(old["result"]["verdict"], "compliant")

        # 变更脉络：C2 -> C1
        status, lineage = self.api.get("/conclusions/C2/lineage")
        self.assertEqual(status, 200)
        self.assertEqual([c["conclusion_id"] for c in lineage], ["C2", "C1"])
        self.assertEqual(lineage[1]["supersede_reason"], None)

        # 新版计算明细可查，撤回样本出现在 excluded
        status, detail = self.api.get("/conclusions/C2")
        self.assertEqual(status, 200)
        excluded = {x["sample_id"]: x["reason"] for x in detail["result"]["excluded"]}
        self.assertEqual(excluded.get("s2"), "withdrawn")
        self.assertIn("s2-fix", detail["evidence"]["sample_ids"])
        self.assertNotIn("s2", detail["evidence"]["sample_ids"])

        # 产线结论列表与后代查询
        status, items = self.api.get("/lines/L1/conclusions")
        self.assertEqual(status, 200)
        self.assertEqual({c["conclusion_id"] for c in items}, {"C1", "C2"})
        status, desc = self.api.get("/conclusions/C1/descendants")
        self.assertEqual([c["conclusion_id"] for c in desc], ["C2"])

    def test_late_sample_is_flagged_then_drives_correction(self):
        c1 = self._issue_first_conclusion()
        # 迟到样本（as_of 之后到齐），先试算确认缺口标注
        self.api.post("/samples", {
            "sample_id": "s-late", "line_id": "L1", "pollutant": "dust",
            "measured_at": "2026-03-01T22:00:00Z", "value": "3",
            "unit": "mg/m3", "received_at": "2026-03-06T00:00:00Z",
        })
        status, trial = self.api.post("/recalculate", {
            "line_id": "L1", "period": "day",
            "anchor": "2026-03-01T12:00:00Z", "rule_id": "R",
            "as_of": "2026-03-03T00:00:00Z",
        })
        self.assertTrue(any(g["code"] == "late_data_pending"
                            for g in trial["gaps"]))
        self.assertNotIn("s-late", trial["included_sample_ids"])

        # 数据补齐后复核，签发更正
        status, c2 = self.api.post("/conclusions/C1/corrections", {
            "reason": "迟到样本 s-late 到齐后补齐复核",
            "issued_by": "regulator-b", "as_of": "2026-03-07T00:00:00Z",
            "conclusion_id": "C-late",
        })
        self.assertEqual(status, 201)
        self.assertIn("s-late", c2["result"]["included_sample_ids"])


class RuleUpgradeApiTests(ApiTestBase):
    def test_rule_upgrade_review_over_api(self):
        self._seed_line_and_rule(limit="10")
        self.api.post("/equipment", {
            "equipment_id": "E1", "line_id": "L1",
            "valid_from": "2026-02-01T00:00:00Z", "phase": "baseline",
        })
        self.api.post("/samples", {
            "sample_id": "s1", "line_id": "L1", "pollutant": "dust",
            "measured_at": "2026-03-01T08:00:00Z", "value": "9",
            "unit": "mg/m3", "received_at": "2026-03-01T12:00:00Z",
        })
        _, c1 = self.api.post("/conclusions", {
            "line_id": "L1", "period": "day",
            "anchor": "2026-03-01T12:00:00Z", "rule_id": "R",
            "rule_version": "1.0", "issued_by": "a",
            "as_of": "2026-03-02T00:00:00Z", "conclusion_id": "C1",
        })
        self.assertEqual(c1["verdict"], "compliant")

        status, _ = self.api.post("/rules", {
            "rule_id": "R", "version": "2.0",
            "effective_at": "2026-04-01T00:00:00Z",
            "limits": {"dust": "8"},
            "canonical_units": {"dust": "mg/m3"},
            "min_samples": 1, "sampling_interval_hours": "4",
        })
        self.assertEqual(status, 200)

        # 用新规则复核
        status, c2 = self.api.post("/conclusions/C1/corrections", {
            "reason": "规则升级 2.0：限值收紧到 8",
            "issued_by": "a", "rule_version": "2.0",
            "as_of": "2026-04-02T00:00:00Z", "conclusion_id": "C2",
        })
        self.assertEqual(status, 201)
        self.assertEqual(c2["verdict"], "non_compliant")
        self.assertEqual(c2["rule_version"], "2.0")
        self.assertEqual(c2["superseding_ids"], ["C1"])

        # 旧结论仍记录旧规则版本与达标判定
        _, old = self.api.get("/conclusions/C1")
        self.assertEqual(old["rule_version"], "1.0")
        self.assertEqual(old["verdict"], "compliant")

        # 脉络包含规则版本字段，便于监管比对
        _, lineage = self.api.get("/conclusions/C2/lineage")
        versions = {c["conclusion_id"]: c["rule_version"] for c in lineage}
        self.assertEqual(versions, {"C2": "2.0", "C1": "1.0"})


if __name__ == "__main__":
    unittest.main()
