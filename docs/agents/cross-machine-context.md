# Cross-Machine Context Guard & Worktree Isolation Policy

> 规范层（Normative Standard）
> 适用范围：`carllx/cross-desk-flow` 双机协作（Windows / Mac）及 Agent 会话

---

## 1. 目标与定位

Cross-Desk Flow 是一个涉及 Windows PC 与 Mac 两台实体机器的跨平台协作项目。
为避免出现 Cross-Project Context Integrity Failure（例如跨项目会话串线、错误污染其他工作区），必须实施严格的上下文守护与 Fail-Closed 机制。

---

## 2. 核心原则与守则 (Core Principles)

1. **唯一权威代码库**：
   - 远程仓库身份：`https://github.com/carllx/cross-desk-flow`。
   - 任何会话启动或运行脚本前，必须校验 `git remote get-url origin` 包含 `carllx/cross-desk-flow`。若指向任何其他项目，**立即 FAIL CLOSED**，严禁执行任何变更。
2. **工作区隔离 (Worktree Isolation)**：
   - 所有重大修改或 Feature 迭代必须在独立的 Git worktree 中进行，禁止污染 canonical repo 工作区。
3. **权威跨机状态 (SSOT)**：
   - 跨机器唯一的权威状态为：GitHub Issues、Pull Requests、已推送到 remote 的 commits 以及 `docs/` 下的规范文档。
   - 禁止依赖跨机 Agent 的口头口述或未持久化的易失性对话。
4. **单步可审计性**：
   - 关键交付必须输出权威 Commit SHA，且确保全套门禁通过后才可推送到 remote。
