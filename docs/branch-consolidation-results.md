# 分支整理执行记录

执行日期：2026-09-27，Asia/Shanghai。依据 [任务文档](branch-consolidation-task.md) 和本次“按照文档清理本项目”请求执行。本文记录本地代码整理，不代表部署、推送或真实设备验收。

## 结果与目录归属

| 工作目录 / 分支 | 整理前 | 整理后 |
| --- | --- | --- |
| `/Users/ufun/code/2026/TTS` / `main` | `0d862b0`；22 个已跟踪修改、16 个未跟踪文件 | 原有 38 个文件均已保存到提交；业务代码验收版本为 `5ae665d`，其后仅补充本记录与文档入口 |
| `/Users/ufun/code/2026/TTS-windows-offline-runtime-v1` | 目录干净，但实际在旧 Mac 分支 `a23867e` | 已切到 `codex/windows-offline-runtime-v1` / `d7a24c3`；切换后跟踪、未跟踪和 ignored 检查均干净 |
| `codex/windows-playback-gap` | `5396416` | 引用保留；唯一补丁已通过 `cherry-pick -x` 整合到 main |
| `codex/mac-runtime-reliability-v1` | `a23867e` | 本地引用保留，已不占用第二个目录；仍是三个保留分支的祖先，无独有提交 |

开始时检查了 Codex 任务列表，未发现其他活跃任务使用第二个目录；相邻的 main 盘点任务为空闲状态。切换没有移动或删除目录，也没有切换 main、reset、clean 或自动 stash。

远端核查仍为 `origin/main=0d862b0`、`origin/codex/windows-offline-runtime-v1=d7a24c3`、`origin/codex/windows-playback-gap=5396416`。本轮没有推送。

提交关系（不把数量当作功能完成度）：

- 整理前 main / Windows 独有提交为 14 / 41；排除补丁等价提交为 9 / 36，分叉点 `e15cda1`。
- 业务验收版本 `5ae665d` / Windows 为 20 / 41；排除补丁等价提交为 15 / 36。最后的文档提交会再增加 main 一项。
- `main...codex/windows-playback-gap` 排除补丁等价提交后，播放分支一侧为 0。五个补丁文件与 `5396416` 逐文件比较一致；cherry-pick 不改变原分支引用，也不把原提交变为 main 的祖先。

## 改动归属与提交边界

| 提交 | 归属与内容 | 验证 |
| --- | --- | --- |
| `1ca906f` | Mac 入口与配置：`start.sh`、Gateway 引擎参数、`voices.json`、Live 音色标签、入口测试、README 与历史诊断校正 | 精确暂存快照：9 项入口、Gateway、启动环境测试；`bash -n start.sh` |
| `e65e4da` | 独立行为策略：Live 与合块 Live 取消库存背压、保留暂停/停止与重试；对应 Live 测试 | 精确暂存快照：29 项 Live、合块与整组测试 |
| `9a3bb18` | 候选批量替换：搜索模块、页面替换逻辑及对应 Node 测试 | 精确暂存快照：搜索与替换测试通过 |
| `1c5ea97` | 人工确认、候选传输和首轮准备：规则审核/连续文本模块、计划/后台合成/Live 复用模块、Studio HTTP、前端审核/准备/整组模块、相应 Python/Node/浏览器检查脚本及文档 | 精确暂存快照：246 项 Python，3 项跳过；5 个 Node 脚本通过 |
| `ebd9699` | 历史 Windows 延迟报告、独立诊断探针、播放说明；注明历史补丁与实测日期 | 内容与原补丁对照；探针在整合后运行 |
| `5ae665d` | 整合 `53964161a3ec272bd9a62756b516a2ba17f9ecc5`：GUI、播放器、增量缓存及两个测试文件 | 完整验证与独立模拟探针，见下节 |

没有按整文件机械拆分共享模块：音色标签、背压策略和首轮状态在 `server/live_session.py` 中精确分开暂存；搜索替换与审核/准备在同一 HTML 中分开暂存。审核与准备共享候选规范化、保存/开播 API 及编辑页面，合为一个依赖完整的提交。原始任务文档及 README 链接随 Studio 文档保存，其历史基线仍可查阅。

本轮没有修改原有业务成果的最终内容；除了整合播放补丁，原始 38 个文件中的代码、测试、配置均与开始时的逐文件 SHA256 一致。文档校正了默认后端、音色资产说明和历史测量归属，并为原任务文档追加本记录入口。

### 单独审查：取消背压

该策略不是首轮准备的技术前提。本轮保留现有未提交成果中的选择，并独立为 `e65e4da`，便于审阅或日后调整：Mac 不因 Windows 高水位、无状态或离线停止合成，旧高低水位配置不再生效，人工暂停/停止仍有效。慢消费或无人消费时库存可能持续增长；这项取舍写入 [当前行为说明](prepared-live.md)，没有默认迁移到 Windows 单机分支。

### 配置与模型资产

Mac 默认启动参数是 auto、4 线程、f16 KV cache、关闭 LLM/Flow Flash Attention；显式 CPU 回退仍可用。历史 CPU 快照保持历史含义，不以新默认值改写旧运行证据。

两个新增音色 `dama-nvzhuang-9-26`、`nvzhuang-fengyi-9-23` 只提交注册信息。本机对应 GGUF 文件存在；未执行真实合成，不宣称另一台机器已具备资产或当前服务已加载音色。

## 整合版本验证

执行平台：macOS / Python 3。业务代码验收提交：`5ae665d924ba8437eb1778a56635d149580416b4`，tree：`975208d614b79b33e21066dad1618cca38b9bcc0`。

- `python3 -m unittest discover -s tests`：运行 251 项，248 项通过，3 项跳过，无失败。跳过项为两个仅 Windows 原生 WinMM 测试，以及一个需显式启用的 launchd 集成测试。
- 对 `tests/test_*.js` 逐个执行 `node`：5 个脚本全部通过，覆盖候选池、准备状态 UI、后台准备交互逻辑、风险和批量替换。
- 播放测试覆盖顺序、播放中下载/会话切换、元数据替换/新增/缺失 WAV、恢复与失败处理、PCM16/Float32、暂停时停止。WinMM 调用在 macOS 使用测试替身，不是声卡实测。
- `git diff --check` 通过；播放补丁的五个文件与来源版本完全一致。
- 独立探针命令：`python3 scripts/windows-playback-probe.py --simulate --output .git/consolidation-20260927/probe --items 1200 --segments 10`。1200 条独立缓存、10 段全部完成；标记为 `device=simulated`，9 次切换中位数约 40.10 ms，最大约 45.82 ms。这只验证模拟路径，不能和 Windows 历史实机数据作性能对照。

本轮没有运行浏览器 fixture 检查脚本、真实推理、Windows 原生播放、安装包构建或部署验收。历史 1557.3 → 69.1 ms 仍归属于 [2026-09-24 的实机记录](windows-playback-switch-latency.md)，不作为新版本的自动验收。

## 保留与删除边界

本轮没有执行文件、目录、分支引用或 stash 删除，没有部署、重启服务或替换正在直播的程序。

- 原始 38 个源码/文档文件的副本、SHA256 清单、tracked patch 位于 `.git/consolidation-20260927/`；精确暂存快照、测试日志和独立探针输出也保留在其中。这些是本地恢复/验证证据，不提交到仓库。
- 保留主目录下原有 `runtime/`、音色资源、模型、缓存、日志、项目数据、截图以及测试产生的隔离产物；没有读取其正文用于清理，也没有批量删除。
- 两个历史 stash 均保留原提交 ID，未 apply/pop/drop：`5b6bc7b51a8164f7a50d114ea507400ca9672d73`（2026-09-14，`pre-origin-main-merge-20260914`）和 `0eed3ef468032d05329696141bdfcd1f8000415a`（2026-09-09，`!!GitHub_Desktop<main>`）。
- 三个远端分支、本地 Windows 单机和播放补丁分支均保留。播放补丁尚未适配 Windows 单机入口，也未完成本轮 Windows 真机验证，因此不提出删除该补丁分支。

唯一待确认删除候选：本地 `refs/heads/codex/mac-runtime-reliability-v1`，旧 HEAD 为 `a23867e3cd1aed5bdb0e20130c7fb97af14d460a`。原因：成果已在所有保留分支中，无独有提交，且 worktree 已移走。影响：仅移除本地分支名称；不会删除目录、提交内容、运行数据或远端引用。依任务文档 B.3，取得该对象明确授权后才执行 `git branch -d codex/mac-runtime-reliability-v1`；本轮先保留。

## Windows 下一轮范围（未实施）

Windows 单机分支保留 `d7a24c3`。已复查 `windows_playback_service.py` 只在首次下载时绑定 `bound_session`，严格播放器另外维护期望序号；不能用 main 播放器整文件覆盖。

| 优先级 | 建议范围 | 必须保留与验收 |
| --- | --- | --- |
| P0 | 生成与播放控制联动、停止后换稿/换音色再次开播 | 显式重绑会话，暂停/继续/停止覆盖无界面服务；不播放旧会话、不要求重启软件 |
| P1 | 移植片间调度优化、失败恢复 | 按方法适配增量索引及状态写入，保留严格会话与序号屏障；不得跳过失败段；Windows 原生 PCM16/Float32 验证 |
| P1 | 完整候选池 | 单机页面当前仍用 `[selectedText(p)]`；贯通全部候选、当前编辑值及筛选 |
| P2 | 首轮准备与复用 | 遵守本地 Ollama/TTS 的 GPU owner；运行文件放 `AI_LIVE_STUDIO_DATA`，不写 Program Files 源码目录；不直接搬入 Mac 并行策略 |
| P2 | 运行状态 | 分开展示合成、播放、库存和等待原因；核对实际 WAV 格式的 RTF |
| 后续 | 人工确认、整组泛化、批量替换 | 按依赖迁移，保留 Windows provider、DPAPI、启动器和运行时管理 |

第一轮建议验收仍为：启动 → 开播 → 暂停 → 继续 → 停止 → 换稿/音色 → 再次开播。无缓存吞吐用 Windows 已有 benchmark 单独测量；首轮库存不能弥补长期产出不足。此范围不因本轮整理自动实施。
