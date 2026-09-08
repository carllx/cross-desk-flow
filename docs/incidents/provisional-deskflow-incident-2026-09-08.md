# Provisional Deskflow Incident Record

> Status: PROVISIONAL / PENDING ARCHITECTURE MIGRATION  
> Date: 2026-09-08  
> Scope: Dual-machine development environment (macOS Server + Windows Client) input and clipboard coordination.

---

## 1. Incident

在同一局域网开发环境中，macOS 作为 Deskflow Server，Windows PC 作为 Deskflow Client 提供键鼠与剪贴板共享。近期出现两起影响开发协作的故障：

1. **UAC / Secure Desktop 控制失效**：当 Windows 端触发用户账户控制（UAC）提权弹窗（安全桌面）或打开以管理员身份运行的终端（Elevated PowerShell / Terminal）时，从 macOS 跨屏切入的鼠标与键盘无法对提权界面进行任何点击或键入。
2. **历史剪贴板风暴（Reported Historical Incident）**：单次在主机上复制文本后，Windows 端的 `Win + V` 剪贴板历史中出现约 8–10 条完全相同的重复记录；鼠标继续跨屏移动时条目持续累积，导致两台主机 UI 严重卡顿、输入响应迟缓。

本记录沉淀该两起故障的只读排查、受控复现验证、根因定位及运维防护边界。

---

## 2. Verified Environment

* **角色拓扑**：
  * macOS: Deskflow Server
  * Windows: Deskflow Client
* **软件版本**：两端均为官方正式构建 **Deskflow 1.26.0.0**。
* **Windows 安装部署**：
  * 现行部署：官方 MSI 安装路径 `C:\Program Files\Deskflow\`（包含 `deskflow.exe`、`deskflow-core.exe`、`deskflow-daemon.exe`）。
  * 历史残留：早期曾存在 `%LOCALAPPDATA%\Programs\Deskflow\deskflow-1.26.0-win-x64-portable\` 便携版部署，已在此次排查前清理完毕（验证目录为空，桌面快捷方式已确认指向 MSI 目标）。
* **传输安全 (TLS)**：TLS 保持启用与证书信任；本轮解决 UAC 问题**全程未禁用 TLS**。
* **网络与连接状态**：Deskflow TCP 控制信道（默认端口 24800）在排查期间正常连通。
  * *注：本轮调查未对物理以太网（直连网卡）与 Wi-Fi 接口进行流量属性归因，不将“有线优先路由已重新验证”作为本次推断事实。*

---

## 3. Verified Observations

### A. UAC 故障现场证据 (Pre-Fix Baseline)

1. **Service 停滞**：Windows 服务的 `Deskflow` 服务状态为 `Stopped`（启动类型为 `Automatic`，服务账户为 `LocalSystem`）。
2. **进程拓扑缺失特权通道**：
   * `deskflow-daemon.exe`（Session 0 服务进程）：**未运行 (PID 0)**。
   * 普通用户 Session 1 中仅存在 GUI 进程 `deskflow.exe` 及其直接派生的低特权子进程：
     `"C:\Program Files\Deskflow\deskflow-core.exe" client`
   * 系统内**不存在**任何由 LocalSystem / Session 0 daemon 派生的 elevated / watchdog core 进程。
3. **功能表现**：普通桌面应用（浏览器、编辑器）操作正常；一旦进入 UAC Secure Desktop 或管理员提权窗口，跨屏输入立即失效。
4. **系统事件日志线索**：
   * Windows Application Log（Event ID 1000, Application Error）记录前期（2026-09-06）曾出现 `NT AUTHORITY\SYSTEM` 账户下 `deskflow-core.exe` 的异常崩溃（故障模块 `ucrtbase.dll`，异常代码 `0xc0000409` - `STATUS_STACK_BUFFER_OVERRUN` / fastfail abort）。
   * `C:\ProgramData\Deskflow\deskflow-daemon.log` 对应时段记录标准异常：`ERROR: standard exception on thread ... resource deadlock would occur`。

### B. Service Mode 恢复后证据 (Post-Fix Verification)

1. **Service 正常常驻**：通过管理员权限受控启动 Windows `Deskflow` 服务后，服务进入持续 `Running` 状态（Session 0，账户 `LocalSystem`，PID: 25296）。
2. **正确跨 Session 进程拓扑建立**：
   * Session 0: `deskflow-daemon.exe`（PID: 25296，ParentPID: 1012 `services.exe`，账户: `LocalSystem`）。
   * Session 1 (用户桌面): 由 daemon 派生并以 `SYSTEM` 特权常驻的 watchdog core 进程 `deskflow-core.exe`（PID: 31596，ParentPID: 25296）。
   * Session 1 (GUI): 用户桌面 GUI `deskflow.exe`（PID: 35980）保持以普通用户权限（`Medium Mandatory Level`）运行，且未重复派生普通桌面 core。
3. **运行指标**：
   * `deskflow-core.exe` 空闲 CPU 占用持续维持在 **0.00%**。
   * Working Set 内存稳定在 **~14.19 MB – 14.61 MB**。
   * 日志无 deadlock，无 `0xc0000409` 崩溃，无异常断连。

### C. 剪贴板受控复现证据 (Clipboard Controlled Test)

1. **静默基准 (Clipboard OFF)**：
   * macOS Server 协商禁用剪贴板共享（`NOTE: clipboard sharing disabled by server`）。
   * 双端零剪贴板事件，网络信道仅交换屏幕切换信息，无 CPU 尖峰。
   * Windows 端确认已开启剪贴板历史记录（`HKCU:\Software\Microsoft\Clipboard\EnableClipboardHistory = 1`）。
2. **受控单次传递 (Single Copy Test)**：
   * macOS 端临时开启剪贴板共享，在 Mac 上单次复制唯一纯文本 Token `CLIP_TEST_20260908_A`（未进行二次复制，无富文本/图像）。
   * **用户 UI 观测**：Windows 端打开 `Win + V` 剪贴板历史，目标 Token **精确出现 1 次**，粘贴至 Notepad 内容完整无误。
   * **底层日志观测**：Windows 端在 8 秒内记录了 13 次密集 `INFO: clipboard was updated`（包含同毫秒 3 连发特征）；macOS Server 观测到来自 Windows 的回弹消息并提示 `mis-sequenced` 后将其丢弃（ignored）。
   * **性能与稳定性**：测试期间 `deskflow-core.exe` CPU 仍维持 0.00%，内存约 14.61 MB，系统 UI 流畅，未引发历史风暴现象。
   * **测试后处置**：验证完毕后两端已立即将 Clipboard Sharing 重新置为 **OFF**。

---

## 4. Root Cause

### UAC / Secure Desktop 控制失效

* **Verified Root Cause**: `Windows Deskflow Service / privileged daemon path was not active.`
* **技术机理**：
  Windows 的用户界面特权隔离（UIPI）与会话桌面隔离机制禁止非提权的普通用户应用向高特权窗口或 Winlogon 安全桌面（Secure Desktop）注入鼠标和键盘输入。当 Windows 的 `Deskflow` 服务处于 Stopped 状态时，系统退回桌面模式（Desktop Mode），由普通用户权限的 GUI 直接运行 core 进程，因不具备跨 Session 及 LocalSystem/UIAccess 特权，必然在 Secure Desktop 下丧失输入能力。只有当 Session 0 服务激活并派生出高特权 watchdog core 进程接入用户会话时，跨屏控制才能接管提权对话框。

### 剪贴板风暴历史故障

* **Root Cause**: `Not fully verified`
* **事实与推论区分**：
  * *Verified Fact*：在当前单一官方 MSI + 正常运行的 Service Mode 纯净环境下，单次纯文本传输不会在 Windows `Win + V` 历史中留下重复条目，未复现历史严重卡顿与性能衰退。
  * *Verified Fact*：Deskflow 底层协议在处理剪贴板更新时存在短时间内的多次并发重发（burst）与双向回弹（rebound），依赖服务端 sequence 校验进行静默丢弃。
  * *Inferred (未经验证的推论)*：历史环境中同时存在的 Portable 便携版与 MSI 安装版、或多次重启未清理干净的僵尸 core/GUI 进程，可能因多实例争抢操作系统剪贴板监听器（Clipboard Format Listener）而形成了恶性事件自激环路（Feedback Loop）。但**严禁**将“便携版残留即剪贴板风暴根因”视为已被证实的事实。

---

## 5. Resolution

1. **彻底清理双重部署残留**：清理 `%LOCALAPPDATA%\Programs\Deskflow` 历史便携文件，固定所有桌面和系统入口指向官方 MSI 安装路径。
2. **保持 Windows Service 持续启用**：确保 Windows `Deskflow` 服务处于 `Automatic` 且为 `Running` 状态，由 Session 0 守护进程管理 elevated core。
3. **保持普通用户 GUI 运行**：日常使用无需也不得以管理员身份启动 `deskflow.exe`，避免破坏权限隔离模型。
4. **剪贴板共享策略收敛**：在 Deskflow 剪贴板双向同步机制与回弹抑制逻辑获得上游完全解释前，日常保持 **Clipboard Sharing = OFF**，杜绝潜在风暴风险。
5. **TLS 持续加固**：保持传输信道 TLS 加密开启。

---

## 6. Human Gate

本次排查经过真实人工操作与系统界面双重验证，验收结果如下：

| 验证项 (Human Gate) | 测试场景 | 验收结果 |
| :--- | :--- | :--- |
| **Elevated Console Gate** | 从 macOS 切入 Windows 管理员提权 PowerShell / Terminal 窗口并进行键鼠操作 | **PASS**（交互流畅无阻断） |
| **UAC Secure Desktop Gate** | 触发系统级 UAC 提权全屏暗化对话框（Secure Desktop）并进行点击与按键操作 | **PASS**（成功点击确认与取消） |
| **Single Clipboard Delivery Gate** | macOS 复制单次测试 Token，Windows 呼出 `Win + V` 检查条目数 | **PASS**（去重显示仅 1 条，内容一致） |

---

## 7. Reusable Troubleshooting Knowledge

未来 Agent 或维护者排查 Windows 端 Deskflow 异常时，请依序执行以下只读检查范式：

1. **检查服务运行状态**：
   ```powershell
   Get-Service Deskflow | Select-Object Name, Status, StartType
   ```
   *若为 `Stopped`，普通桌面可用但 UAC 必挂。启动服务必须具备管理员特权，普通用户执行 `Start-Service` 会直接报错 `Access Denied`。*
2. **检查跨 Session 进程拓扑**：
   ```powershell
   Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^deskflow' } |
     Select-Object Name, ProcessId, ParentProcessId, SessionId
   ```
   *标准健康状态：`deskflow-daemon.exe` 位于 Session 0；`deskflow-core.exe` 位于 Session 1 且其 `ParentProcessId` 等于 daemon 的 PID；`deskflow.exe` 为独立会话普通进程。*
3. **检查安装源唯一性**：
   确认 `%LOCALAPPDATA%\Programs\Deskflow` 无多版本残留，确认快捷方式与注册表服务路径均指向 `C:\Program Files\Deskflow\`。
4. **查看系统崩溃与守护进程日志**：
   - 守护进程日志：`C:\ProgramData\Deskflow\deskflow-daemon.log`（关注是否有 `resource deadlock would occur`）。
   - Windows 应用程序错误日志：Event ID 1000，关注是否存在 `deskflow-core.exe` 的 `0xc0000409` 崩溃。

---

## 8. Current Configuration vs General Rule

必须严格界定现场事实与架构规范的边界，禁止误将局部配置泛化为仓库规则：

| 维度 | 本机当前事实 (Current Machine State) | 通用故障排查原则 (Reusable Principle) | 禁止升级为项目规范 (Not Architecture Policy) |
| :--- | :--- | :--- | :--- |
| **软件版本** | 当前部署为 Deskflow 1.26.0.0 MSI | 安装来源应单一，避免便携版与安装版混部 | **禁止**将 1.26.0.0 锁定为仓库终身依赖版本 |
| **剪贴板共享** | 当前两端均配置为主动关闭 (OFF) | 协议回弹未完全阐明前保持关闭以保稳定 | **禁止**将“永久禁用剪贴板”定性为最终架构设计 |
| **设备角色** | macOS 为 Server，Windows 为 Client | 键盘鼠标源主机为 Server，受控端为 Client | **禁止**假定未来不可切换对调 Server/Client 角色 |
| **权限模型** | Service 以 LocalSystem 运行于 Session 0 | UAC 注入需要特权服务代理，GUI 维持低权限 | **禁止**采用“永久管理员运行 GUI”等旁路方案 |
| **系统协作** | 当前开发机日常使用 Deskflow | 键鼠网络与音频网络彼此独立 | **禁止**将 Deskflow 纳入音频系统控制或配置中心 |

---

## 9. Future Placement

本记录为应急调查形成的暂定性技术归档（`docs/incidents/`）。

后续若项目引入统一的上层治理工程（Umbrella Project / Workspace Integration），或拆分独立的局域网跨屏外设维护工具链，本文件应整体迁移至对应外设套件或工作区运维体系中，不再作为 `desk-audio-bridge` 媒体核心库的直接维护内容。

---

## 10. Scope Guard

> [!IMPORTANT]
> **系统解耦核心边界声明**：  
> 本记录仅作为开发与协作支撑工具的现场故障排查备忘，**绝不得被解释为**：
> 1. 将 Deskflow 代码或运行时并入 `desk-audio-bridge` 控制器架构；
> 2. 允许 `desk-audio-bridge` 音频控制器启停、修改或监控 Deskflow 进程；
> 3. 音频控制器（Audio Controller）在生命周期或传输上依赖 Deskflow；
> 4. 将 Deskflow 配置文件（`Deskflow.conf`）视为音频桥接系统的权威状态（SSOT）；
> 5. 提前决定了未来 umbrella project 或工作区仓库拓扑结构。
>
> 核心边界保持：  
> **`Deskflow is an independent keyboard/mouse coordination system. The audio controller does not control, modify, or depend on Deskflow.`**

---

## 11. Browser Lead Evidence Bundle

* **排查对象**: Windows Deskflow UAC 失效与剪贴板风暴风险
* **UAC 根因判定**: `Verified Root Cause: Windows Deskflow Service / privileged daemon path was not active.`
* **剪贴板根因判定**: `Root Cause: Not fully verified` (历史单次复制产生 8-10 条重复的风暴现象在本次纯净拓扑下未复现；协议层存在 13 次爆发更新与回弹丢弃，当前策略收敛为维持关闭)
* **Human Gate 验证**: 全部 **PASS**（管理员终端可用，UAC 安全桌面可用，测试 Token 精确单次抵达）
* **系统变更状态**: 现场保持纯净 Service Mode 运行，未修改底层 UAC/安全策略，未修改音频核心代码。
