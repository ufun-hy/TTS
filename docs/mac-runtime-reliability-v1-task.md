# Mac 现有运行链路核查与可靠性修复任务

> 本文可直接交给执行智能体。先阅读核查结论，再按任务范围修复；不要把 Windows 单机的新功能整体移植到 Mac。

## 1. 基线、范围与协作

- 运行快照时间：2026-09-15 17:23，Asia/Shanghai；隔离复现于同次检查完成。
- 代码库：`/Users/ufun/code/2026/TTS`。
- 核查分支：`main`；提交：`a23867e3cd1aed5bdb0e20130c7fb97af14d460a`。
- 修复分支：`main`，由用户明确指定，直接在 `/Users/ufun/code/2026/TTS` 执行 Mac 修复；不另建 Mac 修复分支或 worktree。
- 开工前必须确认原目录实际处于 `main`。如果已被其他任务切换到 Windows 分支且存在未提交改动，先与该任务协调保全并隔离改动；不要直接 checkout、reset、stash 或混入 Mac 修复提交。
- 开始时重新核对 HEAD、工作区改动、实际运行入口，不覆盖用户或其他智能体的修改。
- 本任务负责 Mac 服务端的状态真实性、引擎生命周期、健康查询和诊断。Windows 客户端的播放失败策略、Session 绑定及预缓存，在 [Windows V1 任务](windows-offline-runtime-v1-task.md) 中实现。
- 在 `main` 上按修复里程碑提交，只包含本任务改动，不提交 Windows 开发内容；不操作其他项目的进程。直接修改 `main` 不等于授权重启或部署正式服务，正式服务部署按照届时已有授权执行。

## 2. 核查方法与实际运行快照

本轮仅执行源码阅读、只读状态查询、限定字段的进程检查，以及独立 Python 进程内的 fake 模拟。没有读取 Keychain/API Key，没有启动真实推理、播放声音、重启服务或删除文件。

| 项目 | 本轮观察 | 能证明什么 |
| --- | --- | --- |
| Text Studio | PID 75187，实际入口为 `server/text_studio_entry.py`，8770 健康接口返回 200 | 已加载扩展入口；不能只检查基础 `text_studio.py` |
| Audio Cache | PID 75092，8000 返回 200；客户端在线 | 缓存服务可响应，不等于客户端声音播放已经验证 |
| Recording Transcript | PID 75308，8771 返回 `qwen3-asr-1.7b-mlx`、`asr_ready=true` | 当前为 Qwen3-ASR MLX；ready 是依赖、平台和文件预检查，不是本轮真实识别验收 |
| TTS Gateway | PID 75048，项目入口，`--engine-backend cpu`，监听 `*:8765` | 当前默认使用 CPU，不应描述为三个 GPU 模型正在同时运行 |
| 第二个 8765 监听者 | PID 186，监听 `127.0.0.1:8765`；cwd 为 `/Users/ufun/Documents/Codex/2026-09-09/agents-md-skills-agent-agent-agent` | 与项目网关同时占用端口，属于需核实的环境冲突 |
| TTS 健康请求 | `127.0.0.1:8765/health` 和 `localhost` 连接提前关闭；`127.0.0.2` 请求超时 | 当前健康检查异常；不能据此认定 CosyVoice 模型崩溃 |
| Studio TTS 状态 | `tts.ready=false` | 与当前健康请求异常一致 |
| Live Session | `stopped`，7/414 块，客户端缓存 0 秒，客户端在线 | 只是核查时快照，不说明用户此前主动停止还是其他历史原因 |
| Ollama | `/api/ps` 返回空模型列表 | 核查时没有 Ollama 已加载模型；未调用任何 CLI provider 生成 |

以上 PID 是快照，不得作为之后终止进程的依据。日志内存在历史 Metal 断言、旧 Bash 数组错误和 BrokenPipe；它们不自动等于当前仍在发生的根因。

## 3. 对上一轮问题逐项判定

| 编号 | 判定 | 代码证据与影响 |
| --- | --- | --- |
| M-01 | 已确认代码缺陷：健康查询可能被合成阻塞 | `server/tts_gateway.py` 的 `TTSResultCache.get_or_create()` 持有 `_lock` 调用耗时 producer；`stats()` 和 `/health` 也使用这把锁。健康检查会等待推理结束 |
| M-02 | 已确认代码缺陷：终止失败仍报告休眠 | `server/engine_runtime.py` 的 `sleep_if_idle()` 先清空 `_process`；`_terminate()` 吞掉 terminate/kill/wait 失败，调用者仍增加休眠计数并返回成功 |
| M-03 | 已确认代码缺陷：停止状态提前完成 | `LiveSession.stop()` 立即写 `stopped`，实际同步合成可能仍运行。真实入口采用 `SynthesisBlockLiveSession`，该问题同样存在 |
| M-04 | 已确认环境异常，根因尚未完整确定 | 项目网关与另一工作目录进程同时监听 8765；本轮没有处置该进程，也没有证明移除它能消除全部异常 |
| C-01 | 已确认策略，不直接作为 Mac 缺陷修改 | ASR 通过 `_load_model` 的 `lru_cache` 常驻，并在同服务内用 `_asr_lock` 串行执行；没有全局 GPU owner。当前 TTS 为 CPU，本轮未证明 GPU 冲突或 OOM |
| C-02 | 已确认现有分工 | Mac 网页暂停/停止控制生成，Windows GUI 控制播放。现有文档明确该语义；统一控制属于 Windows 单机能力 |
| W-01 | 仓库中已确认，执行端是 Windows | `audio_client/playback.py` 播放异常后继续下一项；`_next_item()` 只从当前 cached 项选最小序号，不保证失败段或缺失段形成屏障 |
| W-02 | 仓库中已确认，执行端是 Windows | 当前播放器按下载时间推断 Session，会标记并物理清理旧会话缓存；没有由 Runtime 显式绑定新 Session 的契约 |
| W-03 | 仓库中已确认，执行端是 Windows | 当前播放器缺少启动/再缓冲时长门槛。30/30/90 是 Windows 新方案的默认值，不是 Mac 已有参数 |
| N-01 | Windows 新增能力，当前 Mac 无此缺陷前提 | Text Studio 尚无 Ollama/通用 API provider，因此“4096 上下文配 8 段批处理”的问题是接入新 provider 时必须防止的风险 |
| N-02 | Windows 新增能力 | 模型包导入、DPAPI、Program Files 安装和远程更新不属于现有 Mac 运行链路 |
| N-03 | 原任务文档事实错误 | Mac 当前模型是 Qwen3-ASR-1.7B MLX，不是 Whisper |

补充：当前 `reset()` 已检查生成线程是否仍存活，存活时拒绝重置。因此 M-03 的已证实问题是状态提前报告，不能夸大为“已证明旧新会话并发运行”。

## 4. 已完成的隔离复现

这些结果来自实际调用当前类的方法；推理、播放、文件落盘均使用 fake，未触碰真实服务。

```json
{"check":"stop_during_synthesis","status":"stopped","worker_alive":true}
{"check":"failed_engine_termination","reported_sleep_success":true,"process_alive":true,"process_reference_retained":false,"reported_state":"sleeping"}
{"check":"health_cache_stats_during_synthesis","stats_returned_while_generation_blocked":false}
{"check":"playback_after_failure","attempted":["first","second"],"states":["playback_failed","played"]}
```

执行智能体应把前三项转为回归测试。第四项由 Windows 任务转换成严格播放模式测试。

## 5. 修复任务

### A. 健康查询与缓存统计解耦（M-01）

1. 让统计读取只持有短时间统计锁，不等待模型加载、合成、文件处理完成。
2. 保留当前合成串行与缓存一致性，不通过移除生成锁引入并发推理或重复写同一缓存。
3. `/health` 返回业务进程、引擎状态和活动请求的真实状态；模型 sleeping/starting 与实际已加载区分。
4. 不把吞吐不足、模型加载中或未知端口服务伪装成 ready。
5. 回归测试：producer 被 Event 阻塞时，缓存统计必须在 200ms 测试预算内返回；健康请求的引擎 probe 使用 fake，确认健康路径不会继承整个合成时长。已有缓存命中/失效测试继续通过。

### B. 引擎退出确认（M-02）

1. `_terminate()` 返回可验证结果，或抛出 `EngineRuntimeError`；只有确认 `poll()` 已结束才认定释放。
2. 优先 terminate，等待 10 秒；必要时仅对本程序持有句柄的子进程 kill，再等待 5 秒。失败保留进程句柄与错误状态。
3. 成功退出后才清除 `_process`、增加 sleep_count。处于停止中或停止失败时不能启动替代引擎。
4. 检查 `sleep_if_idle()`、`close()`、`ensure_ready()` 的所有调用者；保证锁顺序一致，避免唤醒/停止竞态。
5. 生命周期状态增加能表达停止中/失败的值，现有外部引擎模式继续遵循原配置契约；不要借此接管外部进程。
6. 测试覆盖：正常退出、已退出、terminate 失败后 kill 成功、kill 后仍存活、异常退出、并发 wake/sleep。模拟等待，不让单测真实等待 15 秒。

### C. Live Session 停止状态（M-03）

1. 收到 stop 后立即返回 `stopping`，阻止领取下一块；不要求中断当前推理。
2. 基础 LiveSession 和真实使用的合成块子类都在生成线程真正退出时进入 `stopped`。
3. 停止期间完成的当前块不得被送入新会话播放队列；保持现有停止后不继续 enqueue 的约束。
4. 增加可配置停止等待上限，默认 120 秒。超时保留 `stopping`、`stop_timed_out=true` 和明确错误；不能因为超时而宣称释放或允许新会话。
5. reset/start 始终检查实际生成线程，线程仍存活时返回 409。已有检查保留并补测试。
6. 更新网页的活动状态判断、轮询、按钮和提示，覆盖 `stopping`；没有确认线程退出前不可自动 reset/start。
7. Mac 页面明确使用“暂停生成／继续生成／停止生成”的提示，避免暗示远端播放器已经受控。本任务不新增远程播放控制协议。
8. 在快照中新增必要的停止字段，旧字段保留；暂停/继续及循环话术选择的现有行为继续通过回归。

### D. 端口与服务诊断（M-04）

1. 重新枚举 8765 的所有 IPv4/IPv6 监听者、绑定地址、PID、启动时间、程序入口及 cwd；日志和报告不输出环境变量或凭证。
2. 将 TTS Gateway 纳入只读 stack-status 诊断，至少区分“项目进程存在”“有陌生监听者”“健康 HTTP 成功”“引擎可用”。不改原 TTS LaunchAgent 的启动归属。
3. 对带通配地址和回环地址的重叠监听显示明确冲突；不要仅检查某一个 PID 或端口可连通。
4. 不终止 PID 186 或其他陌生进程，不通过扫描后批量 kill，不擅自改正式端口绕开问题。没有对应处置授权时记录环境阻塞，继续完成 A～C。
5. 在隔离端口验证诊断，真实模型验证与当前端口冲突处置分开报告。

## 6. 保持范围与明确不做

- 保留 Mac Qwen3-ASR MLX、已有模型路径、默认 CPU TTS、Keychain 和 launchd 管理。
- ASR 常驻是已有复用策略，本任务不强制改成每次退出，也不增加跨服务 GPU 调度器；没有真实 OOM 证据时不把它列为已发生故障。
- 不自动切换 Metal，不以扩大缓存宣称解决持续吞吐不足。现有 CPU 耗时证据见 [直播诊断](live-playback-diagnosis.md)，新性能结论必须重新测量。
- 不新增 Ollama/在线 provider、VAD、Windows 安装器或更新器；这些属于另一份任务。
- 不改候选话术、商品事实规则、音色、模型、项目文件和用户缓存内容。
- 不清理旧音频、日志或模型。测试产物遵守会话删除授权；不能清理时保留并报告。

## 7. 工程边界与协作顺序

优先在现有模块修复职责内的问题，不新增另一套服务管理系统：

- `server/tts_gateway.py`：健康查询、缓存统计。
- `server/engine_runtime.py`：真实引擎退出和生命周期。
- `server/live_session.py`、`server/live_session_blocks.py`：停止状态。
- `server/stack_services.py`：只读 TTS 诊断。
- `web/text-studio.html` 及实际扩展入口：状态呈现。

建议按健康查询、引擎生命周期、停止状态与诊断三个里程碑提交，每个里程碑带相关测试和文档；同一分支可有多个修复提交。

Windows 任务在自己的独立 worktree 中可以先做 GPU POC；进入 Runtime 集成前，应从 `main` 纳入本任务验证通过的共享模块修复，避免两边分别重写同一生命周期逻辑。Mac 修复始终留在原目录的 `main`，Windows 分支不得占用该原目录。

## 8. 验收与交付

至少运行相关测试集合：

```bash
python3 -m unittest tests.test_engine_launch_environment tests.test_stage1_efficiency tests.test_stage3_efficiency tests.test_live_session tests.test_live_session_blocks tests.test_stack_services tests.test_service_entrypoints
python3 -m unittest discover -s tests -p 'test_text_studio*.py'
node tests/test_text_studio_risk.js
```

新测试必须覆盖 M-01～M-03 的旧行为确实失败、新行为通过，且不得调用真实模型、真实音频设备或读取 Keychain。既有测试若含清理逻辑，遵守本次会话的文件删除边界，不清理用户或正式运行数据。

完成标准：

- 合成期间健康统计不被整段推理阻塞。
- 停止失败时保留进程归属，不能伪报 sleeping/释放。
- 停止合成时状态与线程一致，新会话不会越过未结束线程。
- 实际扩展入口的网页状态正常，既有 Mac 主链路测试通过。
- 真实服务未验证、端口冲突未处置、真实声音未验收等项单独列出；模拟测试通过不能替代真实验证。

交付：修改说明、提交列表、测试结果、环境问题处置状态，以及供 Windows 任务复用的共享提交。不得将“代码修复完成”写成“当前 Mac 实时直播吞吐已达标”。
