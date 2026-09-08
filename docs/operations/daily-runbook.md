# 日常运维与操作手册 (Daily Runbook)

> 适用版本：`v0.1`  
> 权威代码基准：`7bbd0a58a1a25454c502024fcfd632d32fe6bcb1` (`feat/issue-21-production-microphone`)  
> 核心目标：指导日常使用中的服务启停、状态查看与故障排查。

---

## 1. 当前阶段运行操作 (Before #24 / #25)

在自动自启机制（Windows 任务计划程序 #24 与 macOS 启动代理 LaunchAgent #25）落地前，机器重启或登录后不会自动启动媒体服务。用户需通过终端命令行进行显式管理。

### A. Windows 端运维操作

请在 Windows 项目根目录下，使用已验证具备生产依赖（`gstreamer`、`psutil`）的 Python 环境执行：

1. **启动服务 (Idempotent Start)**：
   ```powershell
   python -m windows.cli start
   ```
   - **行为机制**：该命令具备幂等性（Idempotent）。若后台控制器宿主进程尚未运行，它会自动在后台拉起无窗口守护控制器（`python -m windows.cli run`），随后通过本地回环 IPC 发送启动指令并持久化 `DesiredState: ENABLED`；若控制器已在运行，则直接恢复媒体状态。

2. **状态巡检 (Status Check)**：
   ```powershell
   python -m windows.cli status --json
   ```
   - 输出控制器状态、所属进程 PID、对端在线状态（`peer_available`）、当前绑定的直连网络接口及拥有的媒体子进程数量。

3. **主动停止 (Intentional Stop)**：
   ```powershell
   python -m windows.cli stop
   ```
   - **行为机制**：该命令**绝不是简单的 `taskkill` 杀进程**。它会通过本地回环 IPC 通知控制器优雅终止所拥有的媒体子进程，并在本地持久化存储写入 `DesiredState: STOPPED_BY_USER`。在用户下一次显式执行 `start` 之前，任何对端心跳或重启重连均不会偷偷恢复媒体声音传输。

---

### B. macOS 端运维操作

请在 Mac 端终端下，使用已验证的 Conda 运行环境执行（若系统默认 `python3` 具备完全一致的 GStreamer/GObject 绑定，亦可使用）：

1. **启动服务 (Idempotent Start)**：
   ```bash
   /opt/miniconda3/bin/python -m macos.cli start
   ```
   - **行为机制**：同样具备幂等性。若控制器未常驻，会自动拉起独立会话的后台守护进程，并通过本地回环 IPC 发送启动指令并设置 `DesiredState: ENABLED`。

2. **状态巡检 (Status Check)**：
   ```bash
   /opt/miniconda3/bin/python -m macos.cli status --json
   ```

3. **主动停止 (Intentional Stop)**：
   ```bash
   /opt/miniconda3/bin/python -m macos.cli stop
   ```
   - 优雅停用 CoreAudio 扬声器接收与麦克风采集管道，将意图持久化为 `DesiredState: STOPPED_BY_USER`。

---

## 2. 演进后的日常体验 (After #24 / #25)

在后续 #24 (Windows Scheduled Task) 与 #25 (macOS LaunchAgent) 完成后，日常运维模型将自动升级：

- **正常用户无需手动执行命令行**：
  两端机器在登录进入桌面后，操作系统原生托管服务会自动拉起轻量控制器宿主；当双机均连入同一局域网/直连网线时，扬声器链路自动握手进入 Ready 状态。
- **唯一所有权原则保持不变**：
  操作系统计划任务/启动代理仅负责“控制器宿主自身保活（Liveness）”；底层的 GStreamer 媒体子进程**仍然由控制器独占拥有与管理**，严禁由操作系统直接拉起媒体管道。
- **CLI 命令转为运维排错入口**：
  上述 `start`、`stop`、`status` 命令保持完全兼容，作为日常需要临时静音、查看诊断指标或排查网络对齐时的维护排错入口（Maintenance / Troubleshooting Entry Point）。
