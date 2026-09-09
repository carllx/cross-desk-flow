# AGENTS.md

> 仓库维护者规则层（Repository-Maintainer Layer）  
> 读者：IDE Agent 及参与本仓库维护的 Agent  
> 核心问题：IDE Agent 应如何协作与维护 `cross-desk-flow` 仓库？

---

## 1. 仓库定位与双机协作体系 (Dual-Machine Collaboration)

本项目旨在实现同一局域网内 Windows PC 与 Mac 之间的高性能音频、键盘鼠标流转与控制。本项目涉及跨平台双机开发，其协作架构与维护边界如下：

### A. 存储与工作区架构
* **单一权威代码库**：项目唯一远端为 `https://github.com/carllx/cross-desk-flow`。
* **跨机上下文守护 (Cross-Machine Context Guard)**：任何 Agent 启动或任务开始前必须执行 preflight，确保当前仓库与远程 origin 严格匹配 `carllx/cross-desk-flow`，严禁跨项目串线。详见规范文档：`docs/agents/cross-machine-context.md`。
* **独立工作区与 Worktree 隔离**：Windows PC 与 Mac 各自拥有独立的本地 Git workspace。重要迭代必须在独立的 Git worktree 下进行。
* **禁止并发直推 main**：两台机器上的 IDE Agent **严禁**未经协调并发直接向 `main` 分支提交或推送代码。
* **分支与工单驱动**：各机器在独立 Feature Branch / Issue 上开展工作，通过 GitHub PR / Merge 机制最终 Join。
* **跨机权威状态 (Canonical Cross-Machine State)**：GitHub Issue、PR、已推送 Commit 及 `docs/` 文档是跨机器唯一的权威状态（SSOT），绝不依赖 Agent 之间的口头转述或易失会话记忆。

### B. 目录边界划分
* **平台专属区域**：
  * `macos/**`：macOS 端专用逻辑、配置及脚本，由 Mac 侧会话主要负责。
  * `windows/**`：Windows 端专用逻辑、配置及脚本，由 Windows 侧会话主要负责。
* **共享区域**：
  * 根目录文档（`README.md`、`CONTEXT.md`、`AGENTS.md`）及 `docs/**` 属于两端共享状态，修改须慎重，避免未经对齐的并发冲突。

---

## 2. 核心执行守则 (Maintainer Principles)

1. **零新增硬件与低开销契约**：
   - 优先通过成熟开源方案完成 PoC，禁止重复造轮子（如自行编写底层音频驱动）。
   - 核心验收基准为：低 CPU、低内存、低磁盘占用和人耳可接受的低延迟。
2. **外部依赖解耦边界**：
   - **Deskflow**：当前已用于键鼠共享，但保持完全独立，不是 v0.1 硬依赖；严禁 fork 或就地修改 Deskflow。
   - **SonoBus**：作为当前候选传输方案，本阶段严禁提前安装、配置、fork 或 vendor。
   - **BlackHole**：不是 v0.1 必需依赖，严禁提前引入。
3. **代码上下文守护 (Code Context Guard)**：
   - 单源码文件行数严格实行分级限制：<= 600 LOC (Pass), 601–700 LOC (Warn), > 700 LOC (Fail)。
   - 历史存量 > 700 LOC 文件严格执行 No-Worse 规则，禁止净增行数。详见规范文档：`docs/engineering/code-context-guard.md`。
4. **语言契约 (Language Contract)**：
   - GitHub Issue、PR 描述、计划、架构说明、反馈等人类可读材料**默认使用清晰简体中文**；
   - 代码、文件路径、终端命令、API、Git 标识符及技术术语保持英文。
5. **防止过度设计 (Avoid Overdesign)**：
   - 保持最小可用结构，严禁建立当前不需要的复杂抽象层或冗余工程脚手架。

---

## 3. 本地配置与隐私保护规则 (Privacy & Local State)

严禁向 Git 仓库提交以下内容：
* 本机局域网/公网 IP 地址等易变配置（示例请统一写入 `*.example` 模板）。
* 带有用户主目录的本机绝对路径。
* 具体音频硬件 ID / 设备 GUID。
* 密码、Token、Cookies 及各类凭据。
* 本地运行缓存、大型日志及原始性能采样文件（*.dump, *.pcap, *.perf 等）。
* 真实录音文件（*.wav, *.mp3 等）。

配置规范：
* 配置模板纳入版本控制：`*.example` 或模板文件。
* 真实本机配置文件统一忽略：`*.local` 或加入 `.gitignore`。

---

## Agent skills

### Issue tracker

GitHub issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical label mapping. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context. See `docs/agents/domain.md`.

### Context & Size Guards

- Cross-Machine Context Guard: `docs/agents/cross-machine-context.md`
- Code Context Guard: `docs/engineering/code-context-guard.md`
