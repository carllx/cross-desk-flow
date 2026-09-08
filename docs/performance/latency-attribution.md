# 端到端延迟归因与声学基准分析报告 (Cross-Host Latency Attribution Benchmark)

> 关联 Issue: [#34](https://github.com/carllx/desk-audio-bridge/issues/34)  
> 权威生产基线版本 (Authoritative Base Ref): `7bbd0a58a1a25454c502024fcfd632d32fe6bcb1`  
> 测量执行原则: `Measure first. Change nothing.` 严禁通过修改生产代码或临时优化配置干预测试。

---

## 1. 实验目标与总体归因结构 (Executive Attribution Overview)

用户在双机局域网架构（Mac 内置麦克风采集并流式发送 → Windows CABLE Output → 输入法/ASR 引擎）的实际体验中，感知到约 5 秒的语音输入延迟。

本次跨机自动化协同基准测试将端到端全链路拆解为可独立观测、高精度时钟对齐的测量阶段，最终归因结论如下：

```
+-------------------------------------------------------------------------------------------------------------------------+
| 用户端到端体感延迟 (End-to-End User Perceived Latency)                                                                  |
+-------------------------------------------------------------+-----------------------------------------------------------+
| 1. 物理音频传输链路 (Physical Bridge): [MEASURED]            | 2. Windows 输入法截断与定稿阶段                           |
|    Mac 麦克风物理声学事件 -> Windows CABLE Output             |    (Windows input-method endpointing/finalization stage): |
|    - Median: 397.45 ms                                      |    [ESTIMATED: 约 814.67 ms (derived from median          |
|    - p95: 422.99 ms                                         |     subtraction)]                                         |
|                                                             |    - Speech-End -> Final Text Median: 1212.12 ms          |
+-------------------------------------------------------------+-----------------------------------------------------------+
```

* **物理传输链路 (Physical Bridge)**：真实延迟约为 **397.45 ms**（状态：`MEASURED`），远低于用户感知的数秒级延迟。
* **后段输入法处理链路 (Post-Bridge Windows input-method endpointing/finalization stage)**：占据了正常发音结束到最终文字落盘的主要耗时（状态：`ESTIMATED`，约 **814.67 ms**，derived from median subtraction）。
* **初始 3.84s / 16.46s 异常长尾现象**：经离线语义审计，确认为早期索引错位与事件匹配 artifact，而非真实系统的稳态物理延迟。

---

## 2. Phase E — 物理声学链路延迟 (Physical Bridge E2E Latency)

### A. 测量对象与方法
* **链路范围**：Mac 内置麦克风物理脉冲（拍手声）→ macOS CoreAudio 采集 → GStreamer RTP L16 发送 → 直连以太网 → Windows 接收端 GStreamer depay/jitterbuffer → Windows VB-CABLE Output 虚拟声卡采集。
* **时钟对齐**：通过独立以太网测试端口进行高频双向 NTP 风格时钟同步，测量双机单调时钟漂移（Drift < 2 ms）并消除基准偏差。
* **匹配算法**：完全基于声学事件时序因果关系的纯数据驱动匹配，**不包含任何 116 ms / 130 ms 先验偏好 (Zero Latency Prior)**。
* **状态标记**：`MEASURED`

### B. 测量数据分布 (Accepted Robust Distribution)
* **有效相干拍手脉冲数 (N)**: 7
* **逐次测量值 (Individual)**: `[378.61, 338.57, 397.45, 398.12, 414.29, 426.63, 388.20] ms`
* **统计指标**:
  * **中位数 (Median)**: `397.45 ms`
  * **95分位数 (p95)**: `422.99 ms`
  * **最大值 (Max)**: `426.63 ms`
  * **标准差 (Std)**: `26.61 ms`

> [!NOTE]
> 录音序列中检测到的 1 个存在复合击打/混响（multi-strike / reverberation）的模糊事件已被隔离留作底层审计，未纳入上述稳健统计分布。

---

## 3. Phase F-C — 真实输入法听写链路实测 (Real WeChat Dictation Path)

### A. 运行环境与权威约束 (Authoritative Run Identity)
* **运行标识 (RUN_ID)**: `phase-f-c-20260907-221343`
* **协议握手确认**:
  * Mac `START_TEST` acknowledged: **yes**
  * Mac `STOP_TEST` completed: **yes**
* **严格隔离与隐私保护**:
  * Windows CABLE Output 录音进程: **未启动 (NOT RUNNING)**（无并发音频竞争）
  * 目标引擎: **真实微信电脑版输入法听写 (Real WeChat Dictation Path)**
  * Mac 原始音频存储: **已禁用 (Raw audio persisted: no)**
  * 真实语音识别文本落盘: **已禁用 (Recognized text contents persisted: no)**

### B. 离线语义审计 (Offline Audit & Alignment)
* **Mac 侧 VAD**: 共检测出 9 个语音片段。经时间轴对齐，其中一句长语句内部因停顿被切分为 Mac #6 与 #7。
* **最终匹配映射**: 形成 8 个高置信度物理语音段与 8 个 Windows 文本更新事件突发（8 high-confidence utterances ↔ 8 Windows text bursts）的精确 1 对 1 映射。
* **尾部伪影排除**: 早期统计中出现的 3.84 s 与 16.46 s 极大值，经事件时间轴核验，为未对齐索引跨段匹配导致的假象（Index/Join Artifact），真实物理系统不存在该异常延迟尾部。

### C. 流式识别特征 (Streaming Behavior)
在全部 8 / 8 个测试语段中，文字均在用户说话尚未结束时就已经开始流式上屏（8/8 utterances began showing text before physical speech end, consistent with streaming recognition）：
* **首字出现相对于发音结束的时间差 (Text Started Relative to Speech-End)**:
  `[-2860.4, -1784.4, -1677.4, -2465.4, -2398.4, -3634.4, -1643.4, -939.4] ms`
* **分析说明**：负值表明输入法具备流式识别（streaming recognition）能力，用户物理发音结束前已持续产生中间识别结果。因此，“用户发音结束后多长时间出现第一个字”不再适合作评估端到端真实延迟的基准指标。

### D. 发音结束至最终文字稳定时间 (Speech-End -> Final Text Stabilization)
评估语音输入体验的核心指标是：用户物理停止说话（Speech End）到屏幕上文字最终定稿稳定（Final Text Event）的时间间隔。

* **状态标记**: `MEASURED`
* **样本数 (N)**: 8
* **逐次测量值 (Individual)**:
  `[1405.62, 1106.62, 1119.62, 1112.62, 1304.62, 1084.62, 1544.62, 1669.62] ms`
* **统计分布**:
  * **最小值 (Min)**: `1084.62 ms`
  * **中位数 (Median)**: `1212.12 ms`
  * **95分位数 (p95)**: `1625.87 ms`
  * **最大值 (Max)**: `1669.62 ms`
  * **标准差 (Std)**: `211.76 ms`

---

## 4. 后链路延迟归因与分析 (Post-Bridge Attribution)

结合 Phase E 与 Phase F-C 的独立测量结果：

$$\text{Post-Bridge Latency} = \text{Speech-End to Final Median} (1212.12\text{ ms}) - \text{Phase E Physical Bridge Median} (397.45\text{ ms}) \approx 814.67\text{ ms}$$

* **状态标记**: `ESTIMATED`
  * The post-bridge Windows input-method endpointing/finalization component is ESTIMATED at ~814.67 ms median (derived from median subtraction).
  * 估算原因为 Phase F-C 测试保持真实输入法环境，未对相同语段同步在底层并发采集 CABLE Output。
* **工程结论**:
  * Windows 输入法截断与定稿阶段（Windows input-method endpointing/finalization stage）构成了发音结束到文字稳定落盘的主要耗时成分（约 814.67 ms，基于中位数差值估算约占 67%）。
  * **限制说明**：本测试严禁在无证据前提下推断“云端 ASR 自身为绝对主要瓶颈”（Cloud ASR itself was measured and is the dominant cause），因为云端传输与模型内部黑盒细节在此阶段未被直接插桩观测。不对 endpointing/finalization 内部细节做未经测量的细分推测。

---

## 5. 宿主负载关联性 (CPU & Runtime Correlation)

在测试期间对宿主系统的负载监控表明：
* **CPU 均值**: 约 `63.4%`
* **CPU 95分位数**: 达到 `100%`
* **状态标记**: `INCONCLUSIVE`（不确定 / 无显著相关）
* **结论原则**: 目前数据无法证明高 CPU 占用与单次延迟波动之间存在确定性因果关联，但亦**严禁断言 CPU 因素已被彻底排除**。

---

## 6. 方法论与证据可追溯性 (Methodology & Evidence Traceability)

本报告所有测量结论均来自自动化测试框架与本地脱敏证据链：

1. **Mac 侧声学检测器 (Mac VAD & Onset Extractor)**:
   * 基于 GStreamer `osxaudiosrc` 原生采集，采用局部中位数绝对偏差（MAD）包络过滤全局冲击声，杜绝单一脉冲抑制；
   * 采用近邻物理事件聚类（Clustering Window = 0.45s）消除单次敲击产生的微观回弹误判；
   * 零音频持久化（Strict Privacy Guarantee，特征提取完毕后立即物理删除临时 PCM）。
2. **Windows 侧高精度事件捕获器 (Windows Composition Event Sniffer)**:
   * 监听 Text Services Framework (TSF) / DOM compositionupdate 与 compositionend 高精度性能时间戳。
3. **双机单调时钟对齐 (Monotonic Clock Alignment)**:
   * 测试前后执行至少 50 轮轻量级 TCP NTP 风格往返测试，记录单调时钟偏差与漂移。
4. **底层证据与执行工件标识 (Primary Local Artifacts)**:
   * `cross_host_benchmark/audio_bridge_latency_paired_authoritative.json`
   * `cross_host_benchmark/mac_peaks_last_outgoing.json`
   * `cross_host_benchmark/mac_speech_timing_latest.json`
   * `cross_host_benchmark/batch_e_mac_paired.py`
   * `cross_host_benchmark/mac_speech_timing_helper.py`
   * 运行时系统任务审计日志: `task-688.log`, `task-771.log`, `task-815.log`, `task-860.log`, `task-967.log`
