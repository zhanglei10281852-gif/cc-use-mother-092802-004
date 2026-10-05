"""SQLite 持久化层：表结构、不可变约束与行存取。

要点：
- 已签发结论（status='issued'）由数据库触发器强制不可更新、不可删除，
  即使绕过服务层直接写库也无法篡改；
- 所有时间以 timeutil.fmt_dt 的规范字符串存储，同一时刻表示唯一；
- 数值（Decimal）以字符串存储，避免浮点误差。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from . import models
from .timeutil import fmt_dt, parse_dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
    line_id         TEXT PRIMARY KEY,
    plant_id        TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    capacity_tonnes TEXT NOT NULL DEFAULT '0',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment_intervals (
    equipment_interval_id TEXT PRIMARY KEY,
    line_id           TEXT NOT NULL REFERENCES lines(line_id),
    equipment_code    TEXT NOT NULL,
    technology        TEXT NOT NULL DEFAULT '',
    capacity_tph      TEXT NOT NULL DEFAULT '0',
    commissioned_from TEXT NOT NULL,
    decommissioned_to TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equipment_line ON equipment_intervals(line_id, commissioned_from);

CREATE TABLE IF NOT EXISTS samples (
    sample_id     TEXT PRIMARY KEY,
    agency_uid    TEXT NOT NULL UNIQUE,
    line_id       TEXT NOT NULL REFERENCES lines(line_id),
    indicator     TEXT NOT NULL,
    value         TEXT NOT NULL,
    unit          TEXT NOT NULL,
    interval_start TEXT NOT NULL,
    interval_end   TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    status        TEXT NOT NULL,
    duplicate_of  TEXT,
    withdrawn_at  TEXT,
    withdraw_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_line_interval ON samples(line_id, interval_start, interval_end);

CREATE TABLE IF NOT EXISTS shutdowns (
    shutdown_id  TEXT PRIMARY KEY,
    line_id      TEXT NOT NULL REFERENCES lines(line_id),
    start_at     TEXT NOT NULL,
    end_at       TEXT NOT NULL,
    reason       TEXT,
    evidence_ref TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shutdowns_line ON shutdowns(line_id, start_at);

CREATE TABLE IF NOT EXISTS rule_versions (
    rule_version_id TEXT PRIMARY KEY,
    code           TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    params         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conclusions (
    conclusion_id   TEXT PRIMARY KEY,
    line_id         TEXT NOT NULL REFERENCES lines(line_id),
    period_type     TEXT NOT NULL,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    rule_version_id TEXT NOT NULL REFERENCES rule_versions(rule_version_id),
    data_cutoff     TEXT NOT NULL,
    status          TEXT NOT NULL,
    corrects_id     TEXT REFERENCES conclusions(conclusion_id),
    totals          TEXT NOT NULL,
    detail          TEXT NOT NULL,
    gaps            TEXT NOT NULL,
    explanation     TEXT,
    created_at      TEXT NOT NULL,
    issued_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_conclusions_period ON conclusions(line_id, period_type, period_start);
CREATE INDEX IF NOT EXISTS idx_conclusions_corrects ON conclusions(corrects_id);

-- 已签发结论不可变：数据库层强制，任何 UPDATE/DELETE 直接失败
CREATE TRIGGER IF NOT EXISTS conclusions_immutable_update
BEFORE UPDATE ON conclusions WHEN OLD.status = 'issued'
BEGIN SELECT RAISE(ABORT, 'issued conclusion is immutable'); END;

CREATE TRIGGER IF NOT EXISTS conclusions_immutable_delete
BEFORE DELETE ON conclusions WHEN OLD.status = 'issued'
BEGIN SELECT RAISE(ABORT, 'issued conclusion is immutable'); END;

CREATE TABLE IF NOT EXISTS audit_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    line_id     TEXT,
    payload     TEXT NOT NULL DEFAULT '{}'
);
"""


def _equipment_from_row(r: sqlite3.Row) -> models.EquipmentInterval:
    return models.EquipmentInterval(
        equipment_interval_id=r["equipment_interval_id"],
        line_id=r["line_id"],
        equipment_code=r["equipment_code"],
        technology=r["technology"],
        capacity_tph=Decimal(r["capacity_tph"]),
        commissioned_from=parse_dt(r["commissioned_from"]),
        decommissioned_to=parse_dt(r["decommissioned_to"]) if r["decommissioned_to"] else None,
        created_at=parse_dt(r["created_at"]),
    )


def _sample_from_row(r: sqlite3.Row) -> models.Sample:
    return models.Sample(
        sample_id=r["sample_id"],
        agency_uid=r["agency_uid"],
        line_id=r["line_id"],
        indicator=r["indicator"],
        value=Decimal(r["value"]),
        unit=r["unit"],
        interval_start=parse_dt(r["interval_start"]),
        interval_end=parse_dt(r["interval_end"]),
        received_at=parse_dt(r["received_at"]),
        status=r["status"],
        duplicate_of=r["duplicate_of"],
        withdrawn_at=parse_dt(r["withdrawn_at"]) if r["withdrawn_at"] else None,
        withdraw_reason=r["withdraw_reason"],
    )


def _shutdown_from_row(r: sqlite3.Row) -> models.ShutdownEvent:
    return models.ShutdownEvent(
        shutdown_id=r["shutdown_id"],
        line_id=r["line_id"],
        start=parse_dt(r["start_at"]),
        end=parse_dt(r["end_at"]),
        reason=r["reason"],
        evidence_ref=r["evidence_ref"],
        created_at=parse_dt(r["created_at"]),
    )


def _rule_from_row(r: sqlite3.Row) -> models.RuleVersion:
    return models.RuleVersion(
        rule_version_id=r["rule_version_id"],
        code=r["code"],
        effective_from=parse_dt(r["effective_from"]),
        params=json.loads(r["params"]),
        created_at=parse_dt(r["created_at"]),
    )


def _conclusion_from_row(r: sqlite3.Row) -> models.Conclusion:
    return models.Conclusion(
        conclusion_id=r["conclusion_id"],
        line_id=r["line_id"],
        period_type=r["period_type"],
        period_start=parse_dt(r["period_start"]),
        period_end=parse_dt(r["period_end"]),
        rule_version_id=r["rule_version_id"],
        data_cutoff=parse_dt(r["data_cutoff"]),
        status=r["status"],
        corrects_id=r["corrects_id"],
        totals=json.loads(r["totals"]),
        detail=json.loads(r["detail"]),
        gaps=json.loads(r["gaps"]),
        explanation=json.loads(r["explanation"]) if r["explanation"] else None,
        created_at=parse_dt(r["created_at"]),
        issued_at=parse_dt(r["issued_at"]) if r["issued_at"] else None,
    )


class Store:
    """SQLite 存取入口。单连接 + 可重入锁，可安全用于多线程 HTTP 服务。"""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def locked(self):
        """把多次读写包进同一把锁（如签发时“读链头 + 写新结论”）。"""
        with self._lock:
            yield

    # ---------------- 产线 ----------------
    def insert_line(self, line_id: str, plant_id: str, name: str,
                    capacity_tonnes: Decimal, created_at: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO lines(line_id, plant_id, name, capacity_tonnes, created_at)"
                " VALUES (?,?,?,?,?)",
                (line_id, plant_id, name, str(capacity_tonnes), fmt_dt(created_at)),
            )

    def get_line(self, line_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM lines WHERE line_id=?", (line_id,)).fetchone()
        return dict(row) if row else None

    # ---------------- 设备投运区间 ----------------
    def insert_equipment_interval(self, e: models.EquipmentInterval) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO equipment_intervals(equipment_interval_id, line_id, equipment_code,"
                " technology, capacity_tph, commissioned_from, decommissioned_to, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (e.equipment_interval_id, e.line_id, e.equipment_code, e.technology,
                 str(e.capacity_tph), fmt_dt(e.commissioned_from),
                 fmt_dt(e.decommissioned_to) if e.decommissioned_to else None,
                 fmt_dt(e.created_at)),
            )

    def list_equipment_intervals(self, line_id: str, start: Optional[datetime] = None,
                                 end: Optional[datetime] = None) -> List[models.EquipmentInterval]:
        sql = "SELECT * FROM equipment_intervals WHERE line_id=?"
        params: list = [line_id]
        if start is not None and end is not None:
            sql += " AND commissioned_from < ? AND (decommissioned_to IS NULL OR decommissioned_to > ?)"
            params += [fmt_dt(end), fmt_dt(start)]
        sql += " ORDER BY commissioned_from, equipment_interval_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_equipment_from_row(r) for r in rows]

    # ---------------- 监测样本 ----------------
    def insert_sample(self, s: models.Sample) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO samples(sample_id, agency_uid, line_id, indicator, value, unit,"
                " interval_start, interval_end, received_at, status, duplicate_of,"
                " withdrawn_at, withdraw_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.sample_id, s.agency_uid, s.line_id, s.indicator, str(s.value), s.unit,
                 fmt_dt(s.interval_start), fmt_dt(s.interval_end), fmt_dt(s.received_at),
                 s.status, s.duplicate_of,
                 fmt_dt(s.withdrawn_at) if s.withdrawn_at else None, s.withdraw_reason),
            )

    def get_sample(self, sample_id: str) -> Optional[models.Sample]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM samples WHERE sample_id=?", (sample_id,)).fetchone()
        return _sample_from_row(row) if row else None

    def get_sample_by_uid(self, agency_uid: str) -> Optional[models.Sample]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM samples WHERE agency_uid=?", (agency_uid,)).fetchone()
        return _sample_from_row(row) if row else None

    def find_active_natural_duplicates(self, line_id: str, indicator: str,
                                       interval_start: datetime, interval_end: datetime
                                       ) -> List[models.Sample]:
        """自然键（产线+指标+采样时段）相同的有效样本，按送达先后排序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM samples WHERE line_id=? AND indicator=?"
                " AND interval_start=? AND interval_end=? AND status='active'"
                " ORDER BY received_at, sample_id",
                (line_id, indicator, fmt_dt(interval_start), fmt_dt(interval_end)),
            ).fetchall()
        return [_sample_from_row(r) for r in rows]

    def list_samples_overlapping(self, line_id: str, start: datetime, end: datetime
                                 ) -> List[models.Sample]:
        """与给定区间有交集的全部样本（含重复与已撤回，供缺口分析）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM samples WHERE line_id=? AND interval_start < ? AND interval_end > ?"
                " ORDER BY interval_start, received_at, sample_id",
                (line_id, fmt_dt(end), fmt_dt(start)),
            ).fetchall()
        return [_sample_from_row(r) for r in rows]

    def list_samples(self, line_id: str) -> List[models.Sample]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM samples WHERE line_id=? ORDER BY interval_start, received_at, sample_id",
                (line_id,),
            ).fetchall()
        return [_sample_from_row(r) for r in rows]

    def mark_sample_withdrawn(self, sample_id: str, at: datetime, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE samples SET status='withdrawn', withdrawn_at=?, withdraw_reason=?"
                " WHERE sample_id=?",
                (fmt_dt(at), reason, sample_id),
            )

    # ---------------- 停机说明 ----------------
    def insert_shutdown(self, sh: models.ShutdownEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO shutdowns(shutdown_id, line_id, start_at, end_at, reason,"
                " evidence_ref, created_at) VALUES (?,?,?,?,?,?,?)",
                (sh.shutdown_id, sh.line_id, fmt_dt(sh.start), fmt_dt(sh.end),
                 sh.reason, sh.evidence_ref, fmt_dt(sh.created_at)),
            )

    def list_shutdowns(self, line_id: str, start: datetime, end: datetime
                       ) -> List[models.ShutdownEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shutdowns WHERE line_id=? AND start_at < ? AND end_at > ?"
                " ORDER BY start_at, shutdown_id",
                (line_id, fmt_dt(end), fmt_dt(start)),
            ).fetchall()
        return [_shutdown_from_row(r) for r in rows]

    # ---------------- 核算规则版本 ----------------
    def insert_rule_version(self, rv: models.RuleVersion) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rule_versions(rule_version_id, code, effective_from, params, created_at)"
                " VALUES (?,?,?,?,?)",
                (rv.rule_version_id, rv.code, fmt_dt(rv.effective_from),
                 json.dumps(rv.params, ensure_ascii=False, sort_keys=True), fmt_dt(rv.created_at)),
            )

    def get_rule_version(self, rule_version_id: str) -> Optional[models.RuleVersion]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_versions WHERE rule_version_id=?", (rule_version_id,)).fetchone()
        return _rule_from_row(row) if row else None

    def find_effective_rule(self, at: datetime) -> Optional[models.RuleVersion]:
        """截至某时刻已生效的最新规则版本（生效时刻、创建时间、ID 依次决胜，保证唯一）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_versions WHERE effective_from <= ?"
                " ORDER BY effective_from DESC, created_at DESC, rule_version_id DESC LIMIT 1",
                (fmt_dt(at),),
            ).fetchone()
        return _rule_from_row(row) if row else None

    def list_rule_versions(self) -> List[models.RuleVersion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rule_versions ORDER BY effective_from, created_at, rule_version_id"
            ).fetchall()
        return [_rule_from_row(r) for r in rows]

    # ---------------- 阶段结论 ----------------
    def insert_conclusion(self, c: models.Conclusion) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO conclusions(conclusion_id, line_id, period_type, period_start,"
                " period_end, rule_version_id, data_cutoff, status, corrects_id, totals,"
                " detail, gaps, explanation, created_at, issued_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c.conclusion_id, c.line_id, c.period_type, fmt_dt(c.period_start),
                 fmt_dt(c.period_end), c.rule_version_id, fmt_dt(c.data_cutoff), c.status,
                 c.corrects_id,
                 json.dumps(c.totals, ensure_ascii=False, sort_keys=True),
                 json.dumps(c.detail, ensure_ascii=False, sort_keys=True),
                 json.dumps(c.gaps, ensure_ascii=False, sort_keys=True),
                 json.dumps(c.explanation, ensure_ascii=False, sort_keys=True) if c.explanation else None,
                 fmt_dt(c.created_at), fmt_dt(c.issued_at) if c.issued_at else None),
            )

    def get_conclusion(self, conclusion_id: str) -> Optional[models.Conclusion]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conclusions WHERE conclusion_id=?", (conclusion_id,)).fetchone()
        return _conclusion_from_row(row) if row else None

    def publish_conclusion(self, conclusion_id: str, issued_at: datetime,
                           corrects_id: Optional[str], explanation: dict) -> bool:
        """草稿 -> 签发。仅允许从 draft 迁移（触发器双重保证），返回是否成功。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conclusions SET status='issued', issued_at=?, corrects_id=?, explanation=?"
                " WHERE conclusion_id=? AND status='draft'",
                (fmt_dt(issued_at), corrects_id,
                 json.dumps(explanation, ensure_ascii=False, sort_keys=True), conclusion_id),
            )
        return cur.rowcount == 1

    def find_issued_head(self, line_id: str, period_type: str, period_start: datetime
                         ) -> Optional[models.Conclusion]:
        """该 (产线, 周期) 更正链的当前链头：已签发且没有被已签发的后继更正。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conclusions c WHERE c.line_id=? AND c.period_type=?"
                " AND c.period_start=? AND c.status='issued' AND NOT EXISTS ("
                "   SELECT 1 FROM conclusions n WHERE n.corrects_id=c.conclusion_id"
                "   AND n.status='issued')"
                " ORDER BY c.issued_at, c.conclusion_id LIMIT 1",
                (line_id, period_type, fmt_dt(period_start)),
            ).fetchone()
        return _conclusion_from_row(row) if row else None

    def find_issued_successor(self, conclusion_id: str) -> Optional[models.Conclusion]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conclusions WHERE corrects_id=? AND status='issued'"
                " ORDER BY issued_at, conclusion_id LIMIT 1",
                (conclusion_id,),
            ).fetchone()
        return _conclusion_from_row(row) if row else None

    def list_conclusions(self, line_id: str, period_type: Optional[str] = None,
                         period_start: Optional[datetime] = None) -> List[models.Conclusion]:
        sql = "SELECT * FROM conclusions WHERE line_id=?"
        params: list = [line_id]
        if period_type:
            sql += " AND period_type=?"
            params.append(period_type)
        if period_start:
            sql += " AND period_start=?"
            params.append(fmt_dt(period_start))
        sql += " ORDER BY period_start, created_at, conclusion_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_conclusion_from_row(r) for r in rows]

    def list_superseded_ids(self) -> set:
        """所有已被已签发结论更正引用的结论 ID（用于标记链上非头节点）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT corrects_id FROM conclusions"
                " WHERE status='issued' AND corrects_id IS NOT NULL"
            ).fetchall()
        return {r["corrects_id"] for r in rows}

    # ---------------- 审计事件 ----------------
    def insert_audit(self, at: datetime, actor: str, action: str, entity_type: str,
                     entity_id: str, line_id: Optional[str], payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_events(at, actor, action, entity_type, entity_id, line_id, payload)"
                " VALUES (?,?,?,?,?,?,?)",
                (fmt_dt(at), actor, action, entity_type, entity_id, line_id,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )

    def list_audit(self, entity_type: Optional[str] = None, entity_id: Optional[str] = None,
                   line_id: Optional[str] = None) -> List[dict]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: list = []
        if entity_type:
            sql += " AND entity_type=?"
            params.append(entity_type)
        if entity_id:
            sql += " AND entity_id=?"
            params.append(entity_id)
        if line_id:
            sql += " AND line_id=?"
            params.append(line_id)
        sql += " ORDER BY event_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "at": r["at"],
                "actor": r["actor"],
                "action": r["action"],
                "entity_type": r["entity_type"],
                "entity_id": r["entity_id"],
                "line_id": r["line_id"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]
