# Windows 已缓存音频的片间调度耗时

2026-09-24 历史验证记录，对应补丁 `53964161a3ec272bd9a62756b516a2ba17f9ecc5`。该补丁只修改 Windows 播放路径，没有改 Mac TTS、Audio Cache 协议、rebuffer 门槛或 backpressure。

本文数字和“全部通过”均来自 2026-09-24 的记录，不是后续整合版本的自动验收。2026-09-27 整理开始时 main 尚未包含此补丁；是否已整合及本轮验证结果以 [整理执行记录](branch-consolidation-results.md) 为准。

## 现场证据

读取正在运行的客户端日志：

`C:\Users\Administrator\AppData\Local\Programs\AI Audio Client\logs\client.log`

现场缓存是 `H:\无人直播软件\.cache`，H 盘为 FAT32。只读采样得到 1170 条记录，其中 540 段待播，15291.1895 秒（约 4.25 小时）库存。

19:21–19:34 的连续 sequence 日志中，旧版 `inter_segment_gap_ms` 常为 1360–2203 ms；该行之后到 PCM16 compatibility 日志，又耗时约 954–1746 ms。转换几乎均为 0 ms。旧字段在选段之后、状态写入和 GUI 回调之前记录，因此低估完整切换开销。

旧路径：设备结束 → 关闭 → 两次结束状态写入 → 回调 `stats()` 扫描 JSON/检查文件 → `_next_item()` 再扫描 → 两次开始状态写入 → 回调再扫描 → 三次 PCM16 检查 → MCI open/set/play。

对真实直播缓存只读测量 `_items()`，旧代码四次用时 827、1099、693、506 ms；新代码首次读入 239 ms，随后三次 16.3、18.1、12.7 ms。没有通过实例化控制器操作直播缓存，没有重置在播状态。

## 修改

- `playback_cache.py` 负责增量读取：目录枚举获取文件信息，未改变的 JSON 复用已解析内容；新文件、替换文件和缺失 WAV 仍被重新识别。播放器写入后显式失效索引，避免 FAT32 时间戳精度影响自己的状态转换。
- `playback.py` 的事件统计使用现有队列快照，去除片间回调触发的全量 I/O。`gui.py` 的定时刷新显式读取增量索引，保持下载库存显示更新。
- 开始/结束状态与时间戳合并写入，正常播放每段由四次写入降至两次；PCM16 检查由三次降至一次。
- 保留 MCI 后端及顺序播放；补充各阶段计时。`perf_counter()` 在本机 Python 3.11 上提供比旧 `monotonic()` 更细的测量精度。

## 同机 WinMM 对照

使用同一 Windows、H 盘 FAT32、1200 条全部提前缓存的测试记录、10 段静音 PCM16 WAV。每段 250 ms，现有直播客户端保持运行。旧/新实现均由同一探针直接包裹原生 MCI 命令，使用相同 `perf_counter()` 边界计时；不拿旧版日志字段与新版字段直接相减，也未使用假 MCI。

| 指标 | 修改前 | 修改后（最终源码） |
| --- | ---: | ---: |
| 9 次 stopped-observed → next-play 中位数 | 1557.3 ms | 69.1 ms |
| 最大切换间隔 | 2390.2 ms | 76.8 ms |
| `_emit` 中位数 | 455.3 ms | 1.4 ms |
| `_items` 中位数 | 212.3 ms | 15.5 ms |
| `_next_item` 中位数 | 215.6 ms | 18.7 ms |
| 单次状态写入中位数 | 1.4 ms | 1.3 ms |

此前两次新实现测试的切换中位数分别为 65.8、58.7 ms，最大 71.2、75.7 ms。主要改善来自移除重复 JSON/文件扫描。旧实现 MCI open/play/close 的中位数分别为 20.8、26.8、0.2 ms，未观察到需要替换播放后端的秒级开销。

这证明同机预缓存条件下的秒级软件切换延迟已显著降低。测量起点为轮询观察到停止，可能漏计约一个 50 ms 轮询周期及调度延迟；音频自身首尾静音、驱动输出延迟不属于此计时。没有做扬声器回录，因此不宣称完全 gapless。

## 复跑及证据

Windows 的源码目录中运行（输出目录使用与缓存相同的分区）：

```powershell
python scripts/windows-playback-probe.py --output H:\audio-switch-diagnostics --items 1200 --segments 10
```

用 `--baseline-source <旧版 playback.py>` 测同一脚本下的旧实现；`--simulate` 仅适用于明确标记的软件模拟测试。探针创建独立目录，不改直播缓存，不连接服务器；测试文件及日志保留。

本次原始证据保留在 Mac 的 `runtime/diagnostics/windows-switch-20260924/`：

- `client-before.log`：现场正在运行的旧客户端日志。
- `live-cache-profile.json`：真实缓存只读测量。
- `windows-before-results.json` / `windows-before-client.log`：旧实现原生 MCI 对照。
- `windows-after-results.json` / `windows-after-client.log`：新实现首次对照。
- `windows-final-summary.txt`：高精度计时版对照。
- `windows-verified-results.json` / `windows-verified-client.log`：最终源码对照及源码 SHA256。
- `windows-tests.log`：Windows 单元测试及原生 MCI smoke test 结果。

最终 Windows 验证 30 项全部通过，无跳过，涵盖顺序播放、播放期间新会话到达、库存显示刷新、增量索引更新、暂停时停止、PCM16/Float32、原生 MCI。Mac 另通过 21 项 Audio Cache 集成测试。最终实测 `playback.py` SHA256 为 `705b108d1b1ace5db7479e3d2feffab0300b1a5cb9ee5d4f1dcdb9fd9e0992e6`。

Windows 测试源码位于 `C:\Users\Administrator\AppData\Local\Temp\audio-switch-20260924`，测试输出在 `H:\audio-switch-diagnostics-20260924`。初次对照输出路径由 PowerShell 以错误编码读取配置产生，实际路径保存在 `windows-before-summary.txt`，同样位于 H 盘，保留未删除。

当前实机验证运行的是独立测试目录中的修改版源码。正在直播的 `AI-Audio-Client.exe` 没有替换或重启；正式客户端需要重新构建并更新后才会应用修改。
