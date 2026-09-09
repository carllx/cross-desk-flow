# Code Context Guard: File Size & Complexity Policy

> 规范层（Normative Standard）
> 适用范围：`carllx/cross-desk-flow` 仓库全量代码及守护检查

---

## 1. 目标与背景

随着跨平台音视频与控制链路的持续演进，单文件代码体量膨胀容易导致认知负荷过高、模块耦合加剧以及上下文窗口溢出。
为了保护架构模块的深度与清晰度，特制定本 Code Context Guard 规范，对新代码与存量代码实行分级度量与严格的门禁规则。

---

## 2. 统计与分级阈值 (LOC Thresholds)

度量基准基于手写有效源码行数（LOC，Lines of Code）：

| 级别 | 阈值范围 (LOC) | 门禁行为 (CI / Preflight Action) |
|---|---|---|
| **Pass** | `LOC <= 600` | 正常通过。推荐架构模块保持在此范围内。 |
| **Warn** | `601 <= LOC <= 700` | 告警通过。提醒开发者关注文件体积，考虑提炼 seam 或子模块。 |
| **Fail** | `LOC > 700` | 阻塞失败。严禁新写或扩展文件超过 700 行。 |

---

## 3. 存量豁免与 No-Worse 规则 (Grandfathered Legacy Policy)

对于在历史版本中已经超过 700 LOC 的存量文件，采取 **No-Worse Rule（不得恶化规则）**：

1. **基准对比权威 (Comparison Authority)**：
   - 必须通过 `--base-ref <SHA>` 显式指定基准 commit（例如 pre-#43 baseline commit `067c2f7d6d515c539853bc96ee90fb11e9cfa9ee`）。
2. **零净增承诺**：
   - 存量超过 700 LOC 的文件在任何 feature 或 refactor 迭代中，其最终 LOC **不得超过** `--base-ref` 对应基准版本的行数。
   - 若功能增加，必须通过 seam extraction（接缝提取）将独立职责剥离为新模块，确保原文件行数净减少或持平。
3. **针对 Issue #43 / #39 / #44 的基准指标**：
   - `windows/controller.py`: 最终 LOC <= 712
   - `macos/controller.py`: 最终 LOC <= 1156
   - `bridge_core/peer_discovery.py`: 最终 LOC <= 742

---

## 4. 豁免与排除范围 (Exclusions)

以下类型的文件自动排除在 Code Context Guard 检查之外：
- 生成文件（Generated files, 如 protobuf 产物、IDL 转换文件）。
- 第三方依赖与 vendor 目录。
- 自动化测试快照（Snapshots）、固定夹具（Fixtures）及金标准数据（Golden test data）。
- 构建缓存、虚拟环境与临时工件（`build/**`, `dist/**`, `.venv/**`, `__pycache__/**`）。
