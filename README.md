# 钢铁排放改造核证

本项目描述钢铁生产线、监测样本与超低排放改造记录。统计结论需要引用原始样本和适用规则。

项目使用产线和监测样本描述排放核证材料，样本记录包含测量时刻、单位与撤回标记。

这是一个从零实现的钢铁产能改造核证后端（纯 Python 标准库，无第三方依赖），把
**产线、设备投运区间、原始监测样本、异常停机说明、核算规则版本**关联起来，支持按不同
统计周期重新计算并标出证据缺口；已签发的阶段结论不可变，新结果通过更正关系引用旧版。

## 目录结构

```
src/steel_audit/
  contracts.py   对外契约（产线、监测样本的最初定义，保持兼容）
  timeutil.py    UTC 时间、统计周期（shift/day/iso_week/month/quarter）、区间运算
  models.py      领域模型与状态常量
  rules.py       核算规则版本参数解析与校验
  storage.py     SQLite 持久化（含“已签发不可变”触发器）
  compute.py     核算引擎：分段、分摊、停机扣减、证据缺口
  service.py     应用服务：接入、重算、签发、更正链、审计
  api.py         标准库 HTTP JSON API
run_server.py    服务启动入口
tests/           unittest 自动化测试
```

## 核心语义

- **数据截止（data_cutoff）**：每次核算只纳入 `received_at <= data_cutoff` 的有效样本。
  迟到样本不追溯已签发结论，而是通过“重算 + 签发新结论”纳入，形成更正链。
- **不可变性**：结论签发（issue）后不可更新、不可删除——服务层无修改入口，
  数据库触发器强制拦截（`tests/test_immutability.py` 验证绕过服务层直接写库也会失败）。
- **更正关系**：签发时自动把该（产线， 周期）的当前链头填入新结论的 `corrects_id`，
  并生成 `explanation` 差异说明：规则是否变化、新增/移除哪些样本、设备分段是否变化、
  总量差值——据此可判定改造效果来自**工艺变化**还是**数据补齐**。
- **分段核算**：周期按设备投运边界切分（改造前后各为一段），每段独立给出
  运行时长、停机时长、分摊排放量与覆盖率。

## 确定处理规则

同一输入必然得到同一输出（`DeterminismTests` 验证逐字节一致）：

- **重复样本**：`agency_uid` 相同且内容一致 → 幂等重放；`agency_uid` 相同但内容
  不一致 → 409 冲突（须撤回原样本后以新编号重报）；自然键（产线+指标+采样时段）
  与有效样本重复 → 标记 `duplicate` 并排除，**先到先采信**，不自动提升。
- **迟到样本**：以 `data_cutoff` 为准，截止后送达不参与本次核算，在证据缺口中
  以 `late_samples_pending` 标出；规则参数 `late_grace_hours` 用于在明细中标记迟到。
- **跨班次/跨周期记录**：按时间重叠比例分摊
  `allocated = value × 与周期重叠秒数 / 采样总秒数`。
- **样本撤回**：仅影响撤回之后的重算；已签发结论保持原样。被撤样本的重复件
  不自动生效，需以新编号重新报送。
- **停机扣减**：停机时段从运行时长中扣除；无说明的停机构成 `unexplained_shutdown`
  缺口；停机时段内仍有样本构成 `samples_during_shutdown` 缺口。

## 证据缺口类型

`missing_samples`（覆盖不足的未覆盖时段清单）、`unexplained_shutdown`、
`samples_during_shutdown`、`late_samples_pending`、`withdrawn_samples`、
`duplicate_samples_excluded`、`no_equipment_interval`、
`samples_outside_equipment_interval`。

## 核算规则参数（rule_versions.params）

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `indicators` | 要求覆盖的监测指标（必填） | — |
| `method` | `measured` 实测加总 / `factor` 产量×因子 | `measured` |
| `emission_indicator` | 排放指标 | `indicators[0]` |
| `output_indicator` | 产量指标 | `output` |
| `emission_factor` | 排放因子（factor 法必填） | — |
| `intensity_denominator` | 强度分母 `operating_hours` / `output` | `operating_hours` |
| `min_coverage_ratio` | 覆盖率阈值 | `0.9` |
| `late_grace_hours` | 迟到宽限（小时） | `24` |
| `intensity_limit` | 强度限值（可选，超限标记 exceedance） | — |

## HTTP API

```
POST /api/lines                                  建产线
GET  /api/lines/{id}                             产线详情
POST /api/lines/{id}/equipment-intervals         登记设备投运区间（同线区间不得重叠）
GET  /api/lines/{id}/equipment-intervals
POST /api/lines/{id}/samples                     样本报送（幂等/重复/冲突规则）
GET  /api/lines/{id}/samples
POST /api/samples/{id}/withdraw                  撤回样本
POST /api/lines/{id}/shutdowns                   登记异常停机说明
POST /api/rule-versions                          发布核算规则版本
GET  /api/rule-versions
POST /api/computations                           按周期重算 → 草稿结论
POST /api/conclusions/{id}/issue                 签发（不可变，自动挂更正链）
GET  /api/conclusions/{id}                       计算明细（分段/分摊/样本清单/缺口）
GET  /api/conclusions/{id}/lineage               变更脉络（更正链 + 每版差异说明）
GET  /api/lines/{id}/conclusions                 产线结论列表（is_current 标记链头）
GET  /api/audit?line_id=&entity_type=&entity_id= 操作审计
```

错误统一为 `{"error": {"code", "message"}}`，状态码：400 校验失败 / 404 不存在 / 409 冲突。

启动服务：

```bash
python3 run_server.py --db steel_audit.db --port 8080
```

## 测试

运行测试：`python3 -m unittest discover -s tests -v`

编译检查：`python3 -m compileall -q src tests`

覆盖的关键场景：

- `tests/test_scenarios.py` — **产线切换**（月中改造分段核算，迟到样本更正可归因于
  数据补齐而非工艺变化）、**样本撤回**（更正链 + 旧版原样保留）、**规则升级复核**
  （新规则重算历史周期并引用旧版结论）
- `tests/test_compute.py` — 跨班次分摊、数据截止、停机扣减、证据缺口、两种核算方法、
  确定性
- `tests/test_samples.py` — 幂等、冲突、重复、撤回及不自动提升规则
- `tests/test_immutability.py` — 已签发结论在服务层与数据库层均不可变
- `tests/test_api.py` — HTTP 全流程冒烟（计算明细、变更脉络、审计、错误码）

所有时间统一 UTC、秒级精度；金额与监测值用 Decimal 计算，输出统一 6 位小数。
