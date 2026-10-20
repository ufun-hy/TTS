# Live TTS Windows 中断诊断（2026-09-17）

基线：本地 `main` 与 `git ls-remote origin refs/heads/main` 均为 `30642c91860225eee870b9fb305fe338e0e4089a`，提交时间 2026-09-17 15:28:21 +08:00。诊断开始时工作区干净。没有创建分支、worktree、PR，没有修改模型、参数、生产服务或播放器实现。

本报告取代 9 月 14 日报告作为当前诊断依据；旧报告中的 30/12 秒水位已经过时。

## 结论与证据等级

**当前现场持续长时间中断的主因已经确认：新内容生产的有效吞吐低于播放消耗，Windows 库存耗尽；耗尽后直接播放新到的一段，形成反复断音。** 不是根据旧报告推断：当前会话的合成日志、缓存元数据、Windows 通过 `/audio/next` 主动上报的状态，以及本次现场带时间戳采样相互吻合。

另有三个独立问题：

| 问题 | 所在层 | 证据与结论 |
| --- | --- | --- |
| `buffering` 被播放器当成 `cached` | Windows 状态解释 | 当前 main 本地复现，首次 12 秒门槛可以被绕过 |
| 已有下一段也有切换空档 | Windows 播放调度 / WAV 准备 | 当前代码和 Mac 模拟 MCI 实测确认软件路径开销；真实 Windows 声卡间隔尚未测到 |
| 已领取但未确认的项目滞留 `processing` | Audio Cache 领取与恢复 | 现场 s0110、s0111 滞留，未见文件下载请求；响应丢失后不能重领的协议缺陷可复现；现场具体失败点还需 Windows 日志确认 |

不能把所有听感停顿都归为同一种问题。几十秒无声有真实 underrun 证据；几十至几百毫秒的片间停顿另有软件路径及音频自带停顿。

## 整条链路的实际行为

1. `server/text_studio_entry.py:46` 使用 `SynthesisBlockLiveSessionManager`。`server/live_session_blocks.py:163` 将相邻话术合成块串行执行：查询容量 → 合成完整 WAV → enqueue → 下一块。没有在前一块尚未完成时并行生产下一块。
2. `server/tts_gateway.py:143` 的 TTS 结果缓存按文字、声音指纹等复用。命中可以快速返回；未命中仍需完整合成。Windows 内容缓存不能消除这里的新内容生成耗时。
3. `server/live_session.py:607` 附加 sequence、session_id、源音频 SHA256、速度和音量。Audio Cache 完成必要处理才进入 `ready`；本次播放速度为 0.9，实际处理系数受默认范围限制为 0.92。
4. `audio_cache/manager.py:124` 将最小 sequence 的 `ready` 改为 `processing` 再返回。`processing` 同时用于音频处理和下载领取，并不等于“Windows 正在播放”。
5. `audio_client/gui.py:275` 的下载线程依次执行 health → fetch → 下载或 blob 复用 → 本地元数据 → ACK。拿到项目后立即循环，不固定每段休眠；仅无项目或错误时等 poll_interval（默认 1 秒）。没有并行下载队列。
6. ACK 确认的是接收完成；服务端随后释放自己的 WAV。Windows 播放器只接受传输状态 `completed` 的项目，因而“文件已经存在”仍不等于“可以播放”：还必须 ACK 成功、状态允许、会话有效。
7. `audio_client/playback.py:243` 单独线程从本地目录选择项目、更新状态、调用 WinMM，完成后再选下一段。网络通常不在两段都已 ACK 的切换路径内。

## 主因：真实 buffer underrun

现场会话为 `97fceebb6e54`。只读采样于北京时间 17:09:37 开始的一组快照显示：

```text
Live status                 running
backpressure_active         false
client_connected            true
client_buffered_segments    0
client_buffered_seconds     0.0
client_playback_status      idle
Audio Cache ready           0
Audio Cache processing      2
Audio Cache failed          0
```

这里 `0 + playing` 仅表示没有未来库存，当前段还可能在播；**`0 + idle` 才支持已无在播段且没有未来可播库存**。不能只看到 buffered_seconds=0 就宣称已经断音。

原始 `runtime/logs/audio-cache.log:385886` 起反复出现 `0 / idle / HTTP 204`。随后第 385913 行 enqueue 201，第 385918 行下载 s0076，第 385920 行 ACK，接着上报 31.110 秒库存，再转入 playing。恢复段 s0076 的服务端时间为 16:34:13.658 入队、16:34:15.797 ACK。这证明该次空档期间下一段尚未成为 Windows 可播放库存，随后新段到来才继续。

旧 HTTP 日志没有时间戳，因此不能把这些行的数量直接换算成精确的无声秒数。本次新增的外部只读采样记录了真实时间，见 `runtime/diagnostics/live-playback-20260917/poll-timeline.jsonl`。其中状态由：

```text
17:10:28.663  0 秒 / idle
17:11:09.241  0 秒 / playing，completed 从 111 增加到 112
```

两次状态变化的观测间隔为 **40.578 秒**。第二次完整观测为 17:11:49.253 idle → 17:12:14.308 playing，间隔 **25.055 秒**。这是客户端上报状态的空窗，包含客户端轮询、服务端状态年龄和本次采样延迟，不是声卡回录测得的波形静音长度。采样持续约 140 秒，共 64 条，间隔还包含 HTTP 请求耗时。

### 新内容供给有多慢

将 CosyVoice 完成时间和 samples，与 Audio Cache 的 created_at、raw_duration 对齐：要求入队在 TTS 完成后 2 秒内，且 samples/24000 与 raw_duration 一致。s0070–s0107 共 38 段匹配：

| 指标 | 本次日志测量 |
| --- | ---: |
| 单段 TTS 耗时最小 / 中位 / 最大 | 46.037 / 53.347 / 70.662 秒 |
| 38 段 TTS 总耗时 | 2057.944 秒 |
| 38 段最终可播放时长 | 1232.539 秒 |
| 有效供给比（播放秒数 / 合成秒数） | **0.599** |
| 相邻 TTS 请求之间的空闲最小 / 中位 / 最大 | 0.399 / 0.488 / 0.798 秒 |

这段观察窗里，生产几乎连续工作，没有长时间被 300/180 背压关住；平均每生产 1 秒可播放内容，需要约 1.67 秒。积累的库存必然下降。不能以“恢复生产了”代替“生产足以补回库存”。

s0090–s0107 的入队间隔为 47.424–71.067 秒，单段最终时长为 24.027–38.204 秒。从 enqueue 创建到服务端收到 ACK 的中位数 2.357 秒（范围 1.484–5.429 秒）。这是处理、等待领取、传输、本地落盘、ACK 的合计，**不是纯网络下载耗时**。

后面的 s0108、s0109 该合计上升到 8.248、26.060 秒，并出现 BrokenPipe；因此不能宣称网络/传输完全没有问题，但它们解释不了前面 38 段已经存在的约 40% 持续供给缺口。

## 为什么 12 / 300 / 180 秒没有解决

### 12 秒本来只管开播，而且播放器未正确执行它

`audio_client/client.py:336` 只要同一 session 存在 `playing/played/paused/playback_failed` 就认定已经开播。后续下载直接标记 `cached`，不再要求 12 秒。原来的 `played` 文件留在目录，所以队列耗尽也不会清除此判定。

`audio_client/playback.py:462` 的状态白名单遗漏 `buffering`，随后走“旧版元数据”的兜底：只要 status=completed 就返回 cached。真实复现：

```text
本地时长                   5.0 秒
原始 playback_status       buffering
_release_startup_buffer     False
播放器解释的状态           cached
_next_item                 选中了这一段
```

已有测试 `tests/test_audio_cache.py:206` 只检查下载侧 metadata 和时长，没有把 `PlaybackController` 接进来，所以测试通过但播放门槛失效。

即使修正这个 bug，本次常见单段本身已超过 12 秒，第一段到达就足以开播。下一段往往要约 50–70 秒后才完成，第一段的约 30 秒仍覆盖不了它。

耗尽后 `_run` 每 0.2 秒重试；一旦新段 ACK 完成即可选中。复现中，先把一段设为 played，随后仅下载 1 秒的新段，也直接得到 cached。**确实会进入来一段播一段。**

### 300/180 是未来库存背压，不是供给保证

当前默认高低水位已经是 300/180。基于 Windows 上报的 `buffering + cached` 时长，不包含当前正在播放的剩余时间，也不包含服务端尚未下载的 ready 库存。

达到高水位后暂停，降到低水位后恢复；这降低生产过量，但没有提高新内容产出率。现场曾上报超过 300 秒库存（观察到 704.378 秒），后来仍耗尽。高水位是每块生产前检查的反馈阈值，含启动、上报、下载积压的滞后，并非全链路硬上限。

本次用到的 synthesis-block 实现已处理“客户端仍连接、当前 session 状态缺失”的恢复，现场也显示 backpressure=false，故不能把当前长断音归为该处旧死锁。连接状态与缓存状态只在 `/audio/next` 上报，长下载/重试超过 10 秒时仍可能暂时变 stale；这是另一个可加重供给延迟的边界。

## 本地有下一段时，实际切换做了什么

`WinMMPlayer.play` 的顺序是：

```text
上一段设备播放结束
→ 下一次 status mode 轮询发现停止（每次 sleep 50ms，调度可能更晚）
→ close 上一段 MCI alias
→ played JSON 落盘
→ GUI callback 调 stats（全目录读取两遍）
→ _next_item（全目录读取两遍）
→ playing JSON 落盘
→ GUI callback 调 stats（再全目录读取两遍）
→ inspect / 必要时 Float32 → PCM16 转换
→ open 新文件 → set time format → play
```

因此即使有下一段，本实现也没有无缝播放承诺。GUI callback 自身只把事件放入队列，但 `stats()` 在播放线程内同步执行，不能认为 UI 工作全部异步。

本地诊断保留原始控制器、真实 JSON I/O、真实转换和 callback，只把 MCI 命令替换成定时假设备（每段模拟播放 123ms，文件的转换工作量仍为 30 秒、24kHz 单声道）。所有下一段事先已在本地且 completed/cached：

| 场景 | 两次“模拟设备结束 → 下段 play 命令”间隔 |
| --- | ---: |
| 10 条记录、PCM16 | 45.742 / 45.281 ms |
| 10 条记录、Float32，尚无派生缓存 | 334.641 / 321.737 ms |
| 1000 条同 session 记录、PCM16，仅前三段待播 | 275.455 / 280.540 ms |

这是 **Mac 软件路径实测，不是 Windows WinMM 或扬声器实测**。真实 MCI open/close 的耗时、Windows 文件系统与调度仍需测量，不能直接套用上表。

同一份 30 秒 Float32 WAV 的单独转换实测：首次 322.537 ms，再次 0.098 ms；硬链接成另一个 item_id 后又需要 345.673 ms。内容 blob 复用不等于 PCM 派生复用：`wav_compat.py` 的派生名包含 source.name，新 item_id 即使共享源 inode，仍是另一个转换缓存键。

**本次当前会话使用变速处理，检查现场 s0110、s0111 实际文件均为 PCM16/24kHz/单声道。** Float32 转换是默认速度直通场景的重要片间风险，不应作为本次 PCM16 长断音的主因。

## 其他问题与排除边界

### 领取后丢失的项目可能永久跳过

17:09 现场 s0110、s0111 已在服务端 processing，文件完整，时长分别 37.071、31.857 秒；在已检查的日志中找不到它们的 `/audio/files/{id}` 或 `/audio/status/{id}` 请求，后续 s0112、s0113 已在继续传送。

`claim_next()` 在发送 HTTP 响应前就把 ready 改为 processing；如果响应丢失，Windows 还没写出该 item 的元数据，就没有 `_recoverable()` 的入口。服务端也没有领取租约或未 ACK 项重发，后续只发另一个 ready 项。最小本地复现：领取 item1 后丢弃结果，再领取得到 item2；重建 manager 后两者仍 processing，再 claim 返回 None。

该缺陷会丢掉可播库存、跳过 sequence、加剧中断。现场“为什么这两个响应未进入客户端恢复”仍需 client.log 证明；不能仅凭服务器 200 就认定客户端下载成功。不要在直播中盲目把全部 processing 改回 ready，否则可能重复播放仍在下载的项目。

### 音频本身也包含停顿

以 SHA256 找到本次 s0074、75、76、105、107 的原始 TTS WAV。10ms RMS 窗、-45dBFS 阈值测得尾部低能量 0.13–0.24 秒，最长连续低能量 0.29–0.54 秒。这不是设备断流，可能是自然句间停顿；测的是原始音频，变速后的精确长度会变化。没有证据据此修改模型或直接裁掉停顿。

### 状态与观测还不够完整

- 同 session 已播放 JSON/WAV 不自动从扫描集合移出，长期直播的目录扫描成本累积；本次没有清理用户数据。
- 播放失败后会记 playback_failed 并继续下一段；服务端 failed=0 不代表 Windows 从未播放失败，因为 ACK 在播放前就完成。
- 下载与播放对 JSON 没有统一的跨线程读改写保护，启动释放与播放状态更新存在覆盖风险；这是代码风险，未作为现场已确认主因。
- 当前客户端日志主要是 received/completed 和 conversion_ms；没有完整 play-start/end、下一段 ready、gap 记录。元数据 playback_updated_at 也只保留最新一次状态，不能恢复所有历史边界。

## 建议的最小修复顺序及文件

本轮仅诊断，以下没有实施。

| 顺序 | 建议 | 预计文件 |
| --- | --- | --- |
| 1 | 修正 buffering 状态解释，旧版兜底只接受真正缺失状态的旧数据；增加下载侧与播放器联动测试 | `audio_client/playback.py`、`tests/test_playback.py`、`tests/test_audio_cache.py` |
| 2 | 耗尽后显式进入 rebuffering，补充准确的 waiting/buffering/playing 显示；可沿用现有门槛，但承认它不能保证低吞吐下的连续性 | `audio_client/client.py`、`audio_client/playback.py`、`audio_client/gui.py` 及对应测试 |
| 3 | 增加可关联的本地时间线，先测 Windows 短间隙；将派生转换提前到下载/准备阶段，避免每段结束后同步转换；复用 prepared PCM 内容 | `audio_client/client.py`、`audio_client/playback.py`、`audio_client/wav_compat.py`、`audio_client/gui.py` |
| 4 | 将重复目录全量统计移出段间关键路径，复用已有扫描结果；MCI 轮询仅是误差的一部分，如要求真正 gapless，再单独评估持续输出队列 | `audio_client/playback.py`、`tests/test_playback.py`、`tests/test_winmm_smoke.py` |
| 5 | 让已领取未 ACK 项可按客户端身份重取，处理响应丢失、重试与重复 ACK；恢复需避免重复播放 | `audio_cache/manager.py`、`audio_cache/server.py`、`audio_client/client.py`、`tests/test_audio_cache.py` |

**主因不能靠上述状态修补彻底消除。** 在不改模型/性能参数的前提下，实际可选的供给方案是让确定的静态话术在开播前完成可复用音频准备，或者只播已经准备好的内容范围。随机候选组合会影响块级缓存命中，动态时间不能提前固定，需要分别设计。若要求无限时长、持续新内容、立即开播三者同时成立，则本次约 0.599 的供给比不满足需求，必须另行解决有效吞吐。这是修复方案的范围边界，不建议继续单纯提高缓存秒数。

建议诊断事件使用同一客户端单调时钟：`item_id / sequence / downloaded_at / ack_ready_at / prepared_at / play_command_at / stopped_observed_at / next_id / next_ready / buffered_seconds / inter_segment_gap_ms`。下载 ready 与 ACK ready 分开，MCI 命令边界与实际声音边界分开；真实听感间隔最终用 Windows 回环录音核验。服务器日志还需补时间戳与 item_id，避免只靠 HTTP 状态猜测。

## 验证与剩余限制

- 现有相关测试共运行 56 个：54 通过、2 个 Windows 原生测试因当前为 macOS 跳过。覆盖 playback、audio_cache、live_session、live_session_blocks、wav_compat、winmm_smoke。
- 补充本地诊断复现：buffering 绕过、耗尽后 1 秒段立即 cached、Float32 首次及跨 item 转换、目录规模导致的片间开销、领取响应丢失不能重取。
- 可复跑的临时探针：`runtime/diagnostics/live-playback-20260917/probe.py`。结果在同目录 `probe-edmdgq_c/results.json`，包含原始 MCI 模拟命令时间线；运行数据与源码分离，未清理原有文件。
- 保存了 `session-metadata.json`、`matched-tts.json`、`live-snapshot.json`、`first-underrun-excerpt.log`、`source-silence.json` 和 `poll-timeline.jsonl`，没有复制话术文本或凭证。
- 用户确认问题当时仍在发生，并提供局域网 SSH 方向。根据客户端请求找到 192.168.3.106，但 `ufun@192.168.3.106` 登录被拒绝，尚待正确用户名或已有连接命令。尚未读取 Windows 文件系统/client.log，也未核实在跑的 EXE 是否对应该 main。因此本报告没有把 Mac 模拟结果当成 Windows 原生实测，也没有声称已经逐次定位所有短停顿。
