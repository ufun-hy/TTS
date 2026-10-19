# Windows 单机化重构阶段总结与测试交接报告

**项目**：TTS / AI Live Studio  
**仓库**：https://github.com/ufun-hy/TTS  
**工作分支**：`codex/windows-offline-runtime-v1`  
**整理日期**：2026-10-19  
**阶段结论**：功能代码已分批提交；**尚未完成 Windows 实机集成验收，不应直接开始新一轮大规模功能开发或宣布重构完成**。

> 本报告根据仓库中已提交的分阶段实施文档及本轮 GitHub 分支核对整理。它是代码变更总结和测试交接，不是测试通过报告。

## 1. 背景与已经确认的产品决策

原先 Mac 提供生成服务、Windows 下载并播放的链路，正在改造为 Windows 单机使用。用户明确要求：

1. Windows 应交付真正的桌面客户端，而不是启动后只跳转外部浏览器。
2. 主窗口关闭不停止后台任务；用户通过托盘恢复工作台，明确退出才停止整套服务。
3. **音频生成**与**直播播放**是两个独立业务功能，不共用一个开始/停止按钮。
4. 生成的 WAV 必须进入独立持久化库存；直播可在不运行 CosyVoice3 的情况下读取已有库存。
5. 系统/服务重启后可以恢复库存和播放任务，但**绝不自动开播**。
6. 优先保证直播连续播放，后续才评估能否稳定提前生成 6–12 小时库存。
7. 沿用既有 Windows 目标分支；未经确认不创建新分支或 worktree，不修改 `main`。

## 2. 本阶段交付总览

| 阶段 | 交付范围 | 主要代码 | 当前判定 |
|---|---|---|---|
| 1. 桌面客户端 | WebView2 工作台、系统托盘、窗口关闭不停止 Runtime | `windows_launcher.py`、`desktop_shell.py`、Windows 打包文件 | 已提交，待实机构建和 GUI 验收 |
| 2. 生成/播放控制解耦 | 两组按钮、独立控制 API、保留旧 API 兼容 | `server/live_session.py`、`server/text_studio.py`、`web/text-studio.html` | 已提交，待端到端验证 |
| 3. 音频持久化库存 | WAV + 元数据独立保存、查询，ACK/Session 清理不影响原始归档 | `audio_cache/library.py`、`audio_cache/manager.py`、`audio_cache/server.py` | 已提交，待磁盘/崩溃测试 |
| 4. 历史库存回放 | 选择库存条目建立回放 Session，暂停/恢复/停止，复用旧 WinMM | Audio Cache 回放接口、Text Studio 代理和界面 | 已提交，待真实播放验证 |
| 5. 播放任务恢复 | `replay-state.json` 原子保存，重启后恢复为暂停状态 | `audio_cache/replay_journal.py`、`audio_cache/server.py` | 已提交，不等于无损断点续播 |
| 6. 库存容量管理 | 全量容量统计、默认 20 GiB 配额、空间不足拒绝新增归档、手动受保护删除 | `audio_cache/library.py`、Audio Cache / Text Studio API 与界面 | 已提交，待边界/压力测试 |

### 2.1 客户端

原 `windows_launcher.py` 通过 `webbrowser.open(STUDIO_URL)` 打开系统浏览器，仅提供一个简单的 Tkinter 启动窗口。当前改为调用独立的 `desktop_shell.py`，以 pywebview / Edge Chromium (WebView2) 承载原工作台，系统托盘承担隐藏、重新打开和确认退出操作。

**未验证项**：Windows 安装包依赖、WebView2 Runtime、系统托盘行为、严格 UI 单实例及二次打开唤醒。桌面容器不能替代独立 Runtime Host 的完整生命周期验收。

### 2.2 音频生成与直播播放

新增 `/api/generation/start|pause|resume|stop`，生成 Session 支持 `autoplay=False`；新增 `/api/playback/status|start|pause|stop`，播放控制由用户显式触发。旧 `/api/live/*` 暂留自动播放语义，避免一次性破坏旧调用链。

**实现范围**是操作控制权的初步解耦，**并非**完整替换 Audio Cache、重构 WinMM 或 GPU 调度。

### 2.3 音频库存与回放

`AudioLibrary` 将已处理 WAV 和 `metadata.json` 写入 `<audio-cache-root>/library/<audio-id>/`。历史库存可查询并选择，回放将归档 WAV 复制到传输/播放队列，不触发新的 TTS 推理，不覆盖原始文件。

库存查询展示与内部容量统计不同：页面目前只显示最近 100 条音频，全量容量统计扫描归档目录。仍缺少按产品项目分组、分页/持久化播放列表与完整长期库存治理。

### 2.4 重启恢复

`replay-state.json` 记录最近的回放 Session、选择顺序和状态；写入采用临时文件、`fsync` 与原子替换。重启前为 running / paused 的任务恢复为 paused；已停止任务仍为 stopped。用户必须手动继续。

**不承诺**播放器正在播放的某段 WAV 能精确定位续播；服务端 processing、客户端已下载文件与 ACK 的一致性尚未闭环。

### 2.5 容量上限及删除

归档默认上限为 **20 GiB**，由 Audio Cache 进程环境变量 `AI_LIVE_STUDIO_LIBRARY_MAX_GIB` 控制。写入前检查上限与磁盘可用空间；超限拒绝新增归档，**绝不自动删除**。

新增显式删除接口和二次确认，保护正在使用或被回放引用的库存。当前仅管理归档库存，不涵盖 Windows 播放客户端二级缓存，也不具备自动空间回收策略。

## 3. 已新增/修改的测试与阶段材料

### 回归测试文件

- `tests/test_windows_desktop_shell.py`
- `tests/test_generation_playback_split.py`
- `tests/test_audio_library.py`
- `tests/test_library_replay.py`
- `tests/test_replay_journal.py`
- `tests/test_audio_library_capacity.py`

以上为**测试代码已提交**，本次没有执行测试、取得 CI 通过记录或确认 Windows GUI / GPU / WinMM 实测通过。

### 已有分阶段记录

- `docs/windows-desktop-shell-acceptance.md`
- `docs/windows-generation-playback-split.md`
- `docs/windows-audio-library-phase-1.md`
- `docs/windows-library-replay-v1.md`
- `docs/windows-replay-recovery-v1.md`
- `docs/windows-audio-library-capacity-v1.md`

这些文档形成阶段证据链，但部分文件保留了**当时的阶段性限制**。例如初期文档写到“尚未实现库存回放/配额”，其后版本已补充相应代码。应以本报告的跨阶段整理和实际测试结果作为最新状态判断依据。

## 4. 当前尚未完成、需要优先核验的风险

| 优先级 | 风险/缺口 | 为什么重要 |
|---|---|---|
| P0 | 新增功能尚无 Windows 实机端到端通过证据 | 不能确认按钮、接口、Audio Cache、播放器形成真实闭环 |
| P0 | 生成、暂停、停止与播放之间仍可能存在 Session/缓存状态耦合 | 操作独立不等于播放器实际不受影响 |
| P0 | 回放 Session 和原 WinMM 服务的接管/切段行为尚未实测 | 需要确认回放真实有声、顺序正确、停止与再启动不冲突 |
| P0 | 服务重启遇到 processing / 本地已下载 / 已 ACK 音频 | 可能漏播、重播或丢失状态；当前不支持精确断点 |
| P0 | 桌面应用打包依赖、托盘恢复和关闭行为未验证 | 无法认定已完成真正的 Windows 桌面交付 |
| P1 | 独立生成仍采用旧播放缓冲回压逻辑 | 现在不能保证独立预生成 6–12 小时 |
| P1 | 库存持续扫描、写盘、复制回放会增加磁盘 I/O | 长时间运行时需测吞吐、CPU、磁盘占用和延迟 |
| P1 | 配额和删除的竞争条件、异常中断恢复 | 需要检查活跃引用、并发删除、磁盘满额等边界 |
| P1 | UI 单实例、库存按项目分组、分页、播放列表持久化 | 已记录的产品功能缺口，非本轮性能测试前置开发目标 |

**特别关注**：此前历史现场已出现过即使库存较多，Windows 片间切换仍有约 2–3 秒级延迟的情况。那是历史诊断线索，**不是此次代码变更后的新测量结果**。必须将播放器切段时延与合成生成速度分开测试，不能用库存充足推断直播一定流畅。

## 5. 下一轮测试计划（建议按顺序执行）

### T0：先冻结功能和记录基线

- 不继续扩展预生成算法、GPU 调度或播放协议。
- 记录待测 Git Commit、Windows 客户端版本、模型版本、显卡驱动、数据目录和真实库容量。
- 保存一份可回滚的 Windows 安装包及运行数据备份；测试不得覆盖生产库存。

### T1：静态检查、Python 单元测试、API 联调

建议从以下测试开始：

```bash
python -m unittest \
  tests.test_windows_desktop_shell \
  tests.test_generation_playback_split \
  tests.test_audio_library \
  tests.test_library_replay \
  tests.test_replay_journal \
  tests.test_audio_library_capacity
```

然后运行原有 `tests.test_live_session`、`tests.test_text_studio`、`tests.test_windows_playback_lifecycle` 及仓库完整测试。**记录真实 PASS/FAIL，先修阻断问题。**

### T2：Windows 功能闭环（P0）

1. 安装包启动直接展示工作台，不启动外部浏览器。
2. 关闭/恢复桌面窗口时，生成和播放不中断；确认退出按约定停止。
3. 仅生成不播放；仅播放库存不运行 CosyVoice3。
4. 停止生成继续播放库存；停止播放后生成不被直接停止。
5. 勾选多段库存回放，核验顺序和原始 WAV 文件完整性。
6. 回放途中重启 Audio Cache 和客户端：只恢复为暂停，手动继续；记录 processing/ACK 边界。
7. 校验配额超限、容量统计和明确删除后的空间变化；被使用的库存拒绝删除。

### T3：播放性能专项（P0）

记录完整片间时间：上一段真正播完 → 下一段真正出声。至少区分：

- WinMM 关闭/打开/播放调用耗时；
- 播放器线程调度和状态写入耗时；
- 本地磁盘读取与必要格式转换耗时；
- Audio Cache 下载、ACK 与队列调度耗时；
- 已有充足库存时的片间延迟分布（平均/P95/P99/最大值）、空白次数、重播/漏播次数。

必须先确认已缓存条件下的连续性，再归因于 TTS 生产速度。

### T4：生成和系统稳定性专项

在 Windows RTX 3060 12GB 机器上检查：

- 单独 CosyVoice3 推理吞吐、RTF、失败数、模型载入与释放；
- Ollama / CosyVoice3 调度是否造成显存冲突；
- 同时生成与播放时的 CPU、内存、显存、温度及磁盘增长；
- 6–12 小时连续运行中任务存活、队列积压、磁盘写入及播放空白；
- 重启/异常退出恢复时是否安全、不自动播放。

此前的 1000/1000 CosyVoice3 单独压测只能作为**历史参考基线**，不代表新整合链路已通过性能测试。

### T5：再讨论 6–12 小时预生成改造

只有 T1–T4 有客观数据后，才决定是否修改回压逻辑、库存目标小时数、GPU 调度及文件清理策略。**现阶段不要把“可储存 20 GiB”视为“已能持续生成 6–12 小时”。**

## 6. 推荐的验收出口条件

这份交接的推荐门槛为：

- 所有新增测试及相关原有回归均通过，失败项有明确解释与修复记录；
- 桌面窗口、后台驻留、生成/播放独立控制经过真实 Windows 测试；
- 库存 WAV 保存、回放、确认删除及重启暂停恢复经端到端验证；
- 已有足量库存时连续播放无异常中断，并取得可比较的片间时延记录；
- GPU、CPU、内存和磁盘性能有真实数据；
- 对无法做到精确续播、回放任务自动收敛等缺口给出明确不支持/待办结论；
- 测试结束形成 **PASS / PARTIAL / FAIL** 判定，再决定是否进入 6–12 小时预生成实施。

## 7. 当前 Git 状态与执行边界

本报告整理时确认 `codex/windows-offline-runtime-v1` 与 `main` 仍分叉；此前容量管理阶段最新实施提交为 `b50e9f2131e67dec63ddd9387c706646059256f4`。本报告仅作为文档提交，不包含业务代码、播放器或数据库变更，不创建 Git 分支或 worktree，不合并到 `main`。

**建议下一项任务名称**：`Windows 单机化 V2：功能回归与性能验收（测试优先）`。

**最终结论**：前期的功能拆分和库存链路已经形成可测试的代码基础；现在应该暂停扩展，验证真实 Windows 稳定性、播放连续性和资源压力，再决定具体优化方案。
