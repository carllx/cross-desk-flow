# Runtime Performance Budget (Issue #34)

> 状态：**RESOLVED / BASELINE ESTABLISHED**  
> 权威代码基准：`7bbd0a58a1a25454c502024fcfd632d32fe6bcb1` (`feat/issue-21-production-microphone`)  
> 适用范围：Windows PC 与 Mac 双端真实运行时资源开销、链路激活时间及端到端延迟归因分析。

---

## 1. 目标与决策原则 (Decision Principles)

本项目遵循 **Measurement-First（数据先行，零过度设计）** 原则：
- 严禁仅因“进程存在”或“网络有流量”而提前引入激进的静音检测、音频压缩编解码或复杂的生命周期框架；
- 控制器（Controller）常驻与流媒体子进程的资源占用必须在真实物理双机环境中实测验证；
- 只有实测证明某项开销构成不可接受的 CPU、内存、电池或网络损耗时，才建立针对性的优化工单。

---

## 2. 双端全状态资源基准矩阵 (Resource Budget Matrix)

测试环境：
- **物理拓扑**：两机通过直连千兆以太网物理链路连接，辅网 Wi-Fi 保持开启；
- **采样方法**：非侵入式 1 Hz 采样（Windows 采用 `psutil` + 进程/网络计数器，macOS 采用 `psutil` + 物理内存 `vmmap` + 接口流量差值），预热 10s 后连续采样 60s 稳态窗口。

| 状态 / 阶段 | 运行条件 | Windows 资源基准 | macOS 资源基准 | 架构决策 / 状态评价 |
| :--- | :--- | :--- | :--- | :--- |
| **State A: Controller-only**<br>(空闲驻留 / 对端离线) | 无活跃媒体子进程，每秒执行一次轻量发现心跳 (`broadcast_hello`) | **CPU**：~0.79%<br>**RSS**：~30.2 MB (私有 21.0 MB)<br>**有线流量**：Tx ~0.001 Mbps, Rx 0.000 Mbps<br>**线程/句柄**：7 线程 / 210 句柄 | **CPU**：~0.69% (峰值 1.10%)<br>**RSS**：~22.0 MB (物理占用 16.4 MB)<br>**有线流量**：Tx ~0.035 Mbps, Rx 0 Mbps<br>**线程/句柄**：4 线程 / 1 进程 | **MEASURED / 常驻可行**<br>轻量 Python 控制器常驻开销极小，完全满足作为登录常驻进程运行，无需提前设计退避休眠机制。 |
| **State B: Playback Mode**<br>(扬声器正常播放) | Windows 扬声器采集发送，Mac 端扬声器接收播放；麦克风路径未启动 | **CPU**：~3.30% 进程树总 CPU<br>(Controller ~0.78%, Gst ~2.50%)<br>**RSS**：~68.7 MB (私有 34.6 MB)<br>**有线流量**：Tx ~1.648 Mbps, Rx 0.027 Mbps<br>**线程/句柄**：25 线程 / 2,134 句柄 | **CPU**：~2.75% 进程树总 CPU<br>(Controller ~0.33%, Gst ~2.42%)<br>**RSS**：~44.0 MB (物理占用 31.4 MB)<br>**有线流量**：Rx ~1.637 Mbps (约 200 KB/s)<br>**线程/句柄**：20 线程 / 2 进程 | **MEASURED / 可接受**<br>v0.1 保持 Always-ready 待命设计；不引入复杂的空闲自杀/重拉起逻辑。 |
| **State B (Silence)**<br>(扬声器静音空闲) | Windows 扬声器链路保持 RUNNING，但系统无任何声音播放（35s 实测） | **CPU**：~4.60% 进程树总 CPU<br>**RSS**：~75.5 MB (私有 41.6 MB)<br>**有线流量**：**Tx ~1.640 Mbps (持续发送)** | **CPU**：~0.16% 接收子进程<br>**RSS**：内存占用平稳释放<br>**有线流量**：Rx ~0.003 Mbps (网卡自然收敛) | **MEASURED / 已知权衡**<br>Windows 端在静音状态下仍持续发送未压缩 PCM RTP 裸流 (~1.64 Mbps)。此为已知固定带宽成本，但当前局域网与能耗开销不足以阻断生命周期上线。 |
| **State C: Dual-Active (pre-#22)**<br>(当前双向同时激活) | 扬声器链路与麦克风链路同时运行（尚未实现 #22 语音输入独占静音/按需启闭） | **CPU**：~6.32% 进程树总 CPU<br>(Controller ~1.04%, Spk ~3.03%, Mic ~2.22%)<br>**RSS**：~113.7 MB (私有 55.2 MB)<br>**有线流量**：Tx ~1.642 Mbps, Rx ~0.817 Mbps<br>**线程/句柄**：40 线程 / 4,057 句柄 | **CPU**：~4.24% 进程树总 CPU<br>(Controller ~0.29%, Spk ~2.17%, Mic ~1.78%)<br>**RSS**：~78.3 MB (物理占用 43.4 MB)<br>**有线流量**：Rx ~1.637 Mbps, Tx ~0.826 Mbps<br>**线程/句柄**：30 线程 / 3 进程 | **MEASURED / 有边界开销**<br>双向常开总资源可控；但麦克风占用指示灯与隐私语义决定了麦克风链路未来必须交由 #22 实现按需启闭（On-demand）。 |

---

## 3. 链路激活延迟预算 (Activation Latency)

基于本地回环 IPC 命令与真实子进程唤醒测得：

1. **扬声器链路播放激活延迟 (Playback Activation)**：
   - **Windows**：**~21.6 ms**（从接收到对端上线广播通知到 Windows `wasapi2src` GStreamer 进程就绪并汇报 RUNNING）；
   - **macOS**：**~17.97 ms**（IPC `start` 命令触发到 CoreAudio 接收子进程就绪）。
2. **麦克风链路激活延迟 (Microphone Activation)**：
   - **Windows 热启动 (Warm Start, 缓存解析器)**：**13.65 ms**（IPC 往返 12.68 ms，子进程创建并回报 RUNNING 耗时 13.65 ms）；
   - **Windows 冷启动 (Cold Start, 初次设备探测)**：**~8.18 s (8,183 ms)**。
     > [!IMPORTANT]
     > 深度排查证实：8 秒级的冷延迟**完全来自于 Windows 端初次执行 PowerShell `Get-CimInstance` 对 Pack43 / VB-CABLE 驱动的 WMI 枚举与发现**，而非 GStreamer 媒体管道或 RTP/UDP 网络的启动耗时。生产代码必须保持对解析结果的正向缓存，严禁在热路径上反复调用 WMI。
   - **macOS 麦克风启用**：**~16.14 ms**（CoreAudio 默认内置麦克风采集绑定与子进程拉起）。

---

## 4. 稳定性与生命周期特性 (Stability & Lifecycle Discipline)

- **无内存泄漏与失控增长**：
  在连续 60 秒受限采样窗口内，Windows 与 Mac 各状态 RSS 漂移量均低于 0.3 MB，进程内存迅速进入平稳平台期，未见内存发散或句柄泄漏。
- **子进程防复活与单例保护**：
  - Windows 与 Mac 控制器均具备单例锁定机制（端口探测防并发，防二次拉起返回 Exit Code 2）；
  - 发生状态切换或主动 Stop 时，控制器拥有并准确清理自身所属的 GStreamer 子进程 PID，未观察到孤儿子进程（Orphan Children）残留。
- **能耗与发热观察**：
  macOS M1 在双向全流激活时，非特权 `top -stats power` 显示电量影响指数仅处于 0.0 - 1.3 极低区间，调度集中于能效核（E-Cores），无异常发热或风扇异响。

---

## 5. 端到端语音输入延迟归因分析 (Interactive Latency Attribution)

针对日常使用 Windows 语音输入法（如微信输入法）时体感“延迟数秒”的核心问题，本阶段完成了双机声学拍手与真实听写定稿归因评测：

### A. 物理链路网桥延迟 (Phase E Bridge Latency)
- **测量方法**：Mac 物理麦克风近场拾音与 Windows CABLE Output 纯物理时序双机同步比对（排除输入法软件争用）；
- **测量结果**：**MEASURED**（基于 N=7 组严格物理收敛样本）：
  - **中位数 (Median)**：**397.45 ms**
  - **最小值 (Min)**：338.57 ms
  - **P95**：422.99 ms
  - **标准差 (Std)**：26.61 ms
- **结论**：网络传输、双机时钟、JitterBuffer (80ms) 与 GStreamer/WASAPI 管道贡献了约 **0.4 秒** 的物理基线延迟。

### B. Windows 真实输入法出字与定稿归因 (Phase F-C Attribution)
- **测量方法**：Windows 端零音频录制（杜绝与输入法争抢音频硬件），通过 DOM 高精度事件记录微信输入法语音流式出字与定稿时序，与 Mac 物理说话区间进行纳秒级以太网时钟对齐（RUN_ID: `phase-f-c-20260907-221343`，N=8 组高置信一对一短句）；
- **在线流式出字现象 (Streaming Onset)**：
  - 在全部 8 组测试中，输入法在用户尚未完整说完该句前 **1.0 ~ 3.6 秒** 即已开始在页面上渲染文字流；
  - 负延迟现象证实了现代语音输入法的流式在线识别特性，出字开始早于整句停顿。
- **停顿定稿延迟 (Speech-End -> Final Text Change)**：
  - **端到端跨机总延迟**：**MEASURED**
    - **中位数 (Median)**：**1212.12 ms (约 1.21 秒)**
    - **极值区间**：Min 1084.62 ms，Max 1669.62 ms，Std 211.76 ms
  - **推算的 Windows 输入法端成分 (Estimated Windows-Side Component)**：**ESTIMATED**
    - `跨机最终出字 (1212 ms) - Phase E 网桥中位数 (397 ms)` = **~814.67 ms (约 0.81 秒)**
    - 不确定度区间：±30.9 ms
- **早期异常值澄清**：
  早期测试报告中的 3.84s 与 16.46s 经离线时序审计确认属于将句内换气切分进行粗暴索引配对产生的对齐伪影；严格物理对齐后全部 8 组定稿延迟均收敛在 1.1s ~ 1.6s 窄带区间。
- **核心归因结论**：
  **所谓的数秒级延迟主要由 Windows 微信输入法的断句静音等待窗口（Endpointing Silence Timeout，约 700–800ms）及云端最终文本修正决算贡献，占停顿后总耗时的 67% 以上；物理网桥本身仅占约 33%（~397 ms）。**

---

## 6. 后续工程守则与保护约束 (Guardrails for #24 / #25 / #22)

1. **控制器常驻策略**：
   控制器（Windows Scheduled Task / macOS LaunchAgent）在日常登录后保持常驻，暂不投入资源开发深层休眠退避。
2. **媒体子进程权限收敛**：
   严禁将麦克风链路设计为常驻捕获；日常状态下 Mac 麦克风子进程必须处于终止状态，严格保留至 #22 作为用户显式动作的按需链路。
3. **静音流量保护**：
   扬声器在无声音时发送的 ~1.64 Mbps 裸 PCM 流量作为 v0.1 已知开销予以接受，严禁在此阶段引入复杂的动态自杀/静音检测中间层。
4. **硬件探测缓存**：
   严格保留 Windows Pack43 设备解析器的运行时与静态缓存，杜绝重复调用 WMI/CIM 触发 8 秒冷延迟。
