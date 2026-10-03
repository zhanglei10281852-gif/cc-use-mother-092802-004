# 钢铁排放改造核证

钢厂产线改造后，设备记录、监测样本与核算规则往往**不是同时到齐**。本后端把
产线、设备投运区间、原始监测样本、异常停机说明与核算规则版本关联起来，支持
按不同统计周期、不同 `as_of` 截止时刻重新计算，并显式标出证据缺口；已签发的
阶段结论不可变，更正结果通过 supersede 关系引用旧版。

仅依赖 Python 3.11 标准库（`http.server` + `unittest`）。

## 运行

```bash
# 测试
python -m unittest discover -s tests -v

# 编译检查
python -m compileall -q src tests

# 启动 API（默认 127.0.0.1:8080）
PYTHONPATH=src python -m steel_audit.api --port 8080
```

## 模块

| 文件 | 职责 |
|---|---|
| `contracts.py` | 不可变值对象：产线、设备区间、停机说明、样本、规则版本 |
| `timeutils.py` | 周期边界（hour/shift/day/week/month，三班制对齐 UTC epoch）、区间交切 |
| `store.py` | 内存仓储；写入幂等、冲突拒绝；样本指纹去重 |
| `engine.py` | 纯函数核算：as_of 截止、加权切分、缺口判定、calc_hash |
| `conclusions.py` | 结论签发/冻结/更正链（draft → issued → superseded） |
| `service.py` | 编排层，dict/ISO 字符串入参 coercion |
| `api.py` | JSON HTTP API |

## 确定性处理规则

- **重复样本**
  - 相同 `sample_id`、相同内容重复提交 → 幂等返回；
  - 相同 `sample_id`、内容不一致 → `409 conflict`，禁止静默覆盖；
  - 不同 ID 但内容指纹（产线/污染物/时刻/窗口/值/单位）一致 → 标记
    `suspected_duplicate_of`，核算时保留 `received_at` 最早的一份，其余进入
    `excluded`（reason=`duplicate`）。
- **迟到样本**：样本带显式 `received_at`。核算只采纳 `received_at <= as_of`
  的样本；测量时刻在周期内但尚未到齐的，列为缺口 `late_data_pending`。
  以补齐后的 `as_of` 重算即可区分"工艺变化"与"数据补齐"。
- **跨班次/跨周期窗口**：窗口样本（`covers_from/to`）按与统计桶的重叠**秒数**
  比例切分，先扣除停机与无设备覆盖秒数，再按当时投运设备归因到改造阶段。
- **撤回**：`withdrawn_at <= as_of` 才视为撤回；以更早的 `as_of` 重放历史
  结论时，事后撤回不影响结果（时间旅行语义）。
- **半开区间** `[start, end)`：周期、设备区间、停机区间、监测窗口一律一致。
- **不可变**：结论保存证据快照（样本/设备/停机 ID 集合 + 规则版本）与
  `calc_hash`；`calc_hash` 只依赖证据与规则，**不含 as_of**，因此"仅仅换个
  时间重查"不会产生新版本，哈希变化即证据或规则发生了实质变化。

## 证据缺口（gaps）

`blocker`（结论只能 inconclusive）：`equipment_coverage_gap`、
`insufficient_samples`、`unknown_unit`；
`warning`：`late_data_pending`、`sample_coverage_gap`、
`rule_not_effective_for_full_period`、`sample_without_equipment`、
`no_limit_defined`；
`info`：`phase_switch_in_period`、`shutdown_in_period`、
`sample_withdrawn`、`duplicate_samples_deduped`。

## API 摘要

| 方法/路径 | 说明 |
|---|---|
| POST `/lines` `/equipment` `/shutdowns` `/rules` `/samples` | 登记（幂等，冲突 409） |
| POST `/samples/{id}/withdraw` | 撤回样本 |
| POST `/recalculate` | 试算，返回明细与缺口（不签发） |
| POST `/conclusions` | 签发阶段结论 |
| POST `/conclusions/{id}/corrections` | 重算并签发新版，`superseding_ids` 引用旧版 |
| GET `/conclusions/{id}` | 结论 + 完整计算明细（分段、排除项、缺口） |
| GET `/conclusions/{id}/lineage` | 变更脉络：当前 → 旧版 |
| GET `/conclusions/{id}/descendants` | 后续更正版本 |
| GET `/lines/{line_id}/conclusions` | 产线全部结论 |

时间字段一律 ISO-8601 且必须带时区偏移（如 `...Z` / `+08:00`）。

## 典型流程

1. 登记产线、规则 v1、设备区间，提交样本（含 `received_at`）；
2. 以 `as_of=数据当时` 调 `/recalculate` 试算，`POST /conclusions` 签发；
3. 样本撤回/迟到补齐/规则升级后，调 `/conclusions/{id}/corrections` 生成新版；
4. 监管通过 `GET /conclusions/{id}` 查看计算明细，`/lineage` 查看变更脉络。
