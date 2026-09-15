# Windows 单机离线版 V1 开发任务

> 本文可直接交给执行智能体。它整合用户重写的方案、代码核查与补充约定；不是已经实现或验证通过的功能说明。

## 1. 目标、基线与授权边界

目标硬件：Intel i5-12400、RTX 3060 Ti 8GB、32GB RAM；V1 发布目标为 Windows 11 x64，Windows 10 是否支持单独记录，不未经测试承诺。

目标流程：

```text
录音 → Qwen3-ASR → 释放 ASR → 本地话术改写 → 释放 LLM
→ 确认最终稿 → 启动 CosyVoice → 预缓存 → 边生成边播放
```

- 默认离线，单路直播，一次只允许一个本程序 GPU owner。
- 新增通用在线文本 API，保留现有 CLI provider；在线模式由用户明确选择，失败时不自动上传到其他 provider。
- 复用现有业务功能，保留 Mac MLX ASR、CPU TTS 默认值、Keychain、launchd、项目格式及独立 Windows 客户端部署方式。
- 本文允许开发、构建和隔离验证，不自动授权发布 Release、改变正式服务、访问账号凭证或删除用户文件。依已有明确授权执行，未授权事项单列交付状态。

### Git 与两个执行任务的协作

- 核查基线：`main` 的 `a23867e3cd1aed5bdb0e20130c7fb97af14d460a`。开始执行时以最新审核通过的稳定提交为基线，记录完整 SHA。
- 固定分支：`codex/windows-offline-runtime-v1`。
- 固定 worktree：`/Users/ufun/code/2026/TTS-windows-offline-runtime-v1`。
- 原目录 `/Users/ufun/code/2026/TTS` 保留给 Mac 在 `main` 上修复；禁止仅在原目录切换 Windows 分支替代独立 worktree。如果已经发生且有未提交改动，先协调保全、按任务归属隔离改动，再恢复目录安排；不得直接 reset、清理或丢弃改动。
- POC、Runtime、播放、安装更新均沿用同一分支和 worktree；划分里程碑，不限定只能有五个提交。
- 共享生命周期缺陷由 [Mac 修复任务](mac-runtime-reliability-v1-task.md) 在原目录的 `main` 上先修复。Windows GPU POC 可独立先行；集成阶段从 `main` 纳入验证通过的共享修复提交，不复制另一套 ManagedEngine/LiveSession。
- 不自动合并 main；不要把用户尚未提交的更改作为可覆盖内容。

## 2. 先读懂当前实现

- 实际 Studio 入口是 `server/text_studio_entry.py`，包括页面扩展、事实统一、文稿接续、恢复接口和合成块 manager；只启动基础 `text_studio.py` 会遗漏功能。
- 当前 ASR 是 Qwen3-ASR-1.7B MLX，不是 Whisper。模型缓存常驻是 Mac 已有策略。
- 当前 Studio 只有 CLI provider；网页/后端默认批大小为 8，新增本地 4096 上下文模式不能照搬。
- 已有 `WinMMPlayer`、`PlaybackController`、WAV Float32→PCM16 兼容层、download 和 ACK，不重写播放器底层。
- 当前播放器在失败后继续下一段，按下载时间推断 Session，还会清理旧会话文件；单机严格模式必须覆盖这些行为。
- `/speak` 合成后在 Mac 用 afplay 播放，`/synthesize` 返回 WAV；Windows 的试听和直播要走明确的本机播放路径。
- 现有缓存统计和引擎退出存在已复现缺陷，详见 Mac 任务；不能拿它们直接作为 GPU 已释放或健康的证据。

## 3. 工作区、模型与用户数据

### 文件布局

| 类别 | Windows 默认位置 | 规则 |
| --- | --- | --- |
| 程序与随包运行时 | `C:\Program Files\AI Live Studio\` | 常规运行不写入此处；安装升级按系统权限执行 |
| 用户数据 | `%LOCALAPPDATA%\AI-Live-Studio\` | projects、voices、cache、logs、config、runtime、updates 分目录 |
| 模型 | `D:\AI-Live-Studio-Models\` | 首次运行可选择其他磁盘；没有 D 盘时要求选择可用目录，不创建虚假默认配置 |

建议模型目录：

```text
AI-Live-Studio-Models/
  asr/Qwen3-ASR-1.7B/
  llm/Qwen3-8B-Q4_K_M/      # GGUF、Modelfile 与必要配置
  llm/ollama-store/         # 本程序专属 Ollama manifests/blobs
  tts/CosyVoice3-Q8/
  tts/voices/              # 随资源包提供的固定参考资源
```

- worktree 只保存源码、配置模板、测试、文档和模型清单，不复制模型、HF 缓存、Ollama blob、私人音色/录音。
- Mac 开发可通过外部路径复用当前模型；不要运行会把模型装进新 worktree 的原安装脚本。
- 用户自行创建/编辑的音色与参考材料放用户数据目录；固定模型资源与用户音色明确区分。
- 模型清单记录 ID、格式、revision/hash、所需文件、兼容运行时版本；缺失或不兼容明确报错。
- 支持离线模型目录导入和 Ollama 注册；不做自动模型下载器。GGUF 导入可能产生额外 blob，磁盘检查计入这部分，原文件不自动删除。
- 升级、卸载、会话切换不默认删除项目、模型、音色、缓存或配置。

## 4. 里程碑 1：目标 Windows GPU POC

正式产品改造前先形成可在目标机执行的独立测试包。Mac 或无 GPU CI 的模拟测试不能当作 Windows GPU POC 通过。

POC 代码置于 `scripts/windows-poc/`，产物、日志和结果置于外部指定输出目录。包内含必要运行环境或明确的开发运行方式；模型通过参数传入。首次分发即可提供测试包与测试命令，不等最后阶段才考虑目标机运行。

### ASR

- 使用 Qwen3-ASR-1.7B 官方 PyTorch/CUDA 权重；不可直接把 MLX 模型目录当作 Windows 权重。
- batch=1、中文、独立 worker 进程。加载一次处理一份录音，任务完成/失败后退出，确认进程结束。
- 第一版优先使用现有 FFmpeg 的 CPU 静音检测寻找分块边界：目标 20～40 秒，硬上限 60 秒；没有可用静音时按上限切分。
- 默认不启用重叠。若 POC 证明硬切边界明显漏字，可启用受限重叠并补边界合并测试；不得用全文去重消除真实重复话术。
- 保存 chunk 在原音频中的真实起止偏移和原始识别文本，适配当前清洗函数需要的 segments；不伪造词级时间戳。
- 测试连续讲话、长静音、背景音乐、块边界数字/商品名和长录音。记录内容质量与显存峰值后固定切分配置。
- 起步使用标准 PyTorch 注意力与 BF16（目标卡支持且实测通过时），不把额外 FlashAttention 编译设为用户安装要求。

### 本地 LLM

- Ollama＋Qwen3 8B Q4_K_M，4096 上下文、并发 1、关闭思考输出。
- 使用打包的独立 Ollama CLI，独立实例默认监听 `127.0.0.1:11435`，独立模型存储；环境变量只对子进程设置，不修改用户全局 Ollama。
- `OLLAMA_MAX_LOADED_MODELS=1`、`OLLAMA_NUM_PARALLEL=1`、关闭 Ollama 云功能。
- 逐话术单元生成，候选依次产生；输入、指令和预留输出纳入长度预算，超限明确报错，禁止静默截断。
- 识别空输出、错误段落 ID、候选缺失和截断；不要用原文填充冒充生成成功。
- 检查价格、数量、规格、优惠条件和新增承诺；本地结果不声称与在线大模型等效。
- 释放时显式发送卸载请求，检查 `/api/ps` 中目标模型消失；异常时仅处理本程序启动的实例。固定 sleep 不是释放证据。

### TTS 与性能门槛

- CosyVoice3 Q8，复用当前音色、prompt 处理和目标约 180 字的合成块。
- 先验证工程当前引擎版本的 Windows CUDA 构建与匹配 GGML/DLL；记录锁定版本和 hash。出现噪声时核查匹配源码构建，不盲目替换权重。
- 使用一组不同的、真实未缓存话术，覆盖数字、长短句、不同音色；记录冷加载时间、合成耗时、音频时长、GPU 利用率、显存峰值和失败。
- `model_rtf = 未缓存合成总耗时 / 原始音频总时长`，冷启动和缓存命中单列，不用每块 RTF 的简单平均替代时长加权统计。
- 同时记录从任务开始到可播放的端到端耗时，除以调速后的播放时长得到供给 RTF；包含音频处理、落盘和传输。
- 目标：默认 1.0 倍语速、与推流软件同机运行时，未缓存供给 RTF ≤0.67。其他支持语速分别记录吞吐，不将调速前结果直接套用。
- 若达不到，报告真实瓶颈和数据，停止宣称该硬件实时模式验收通过；不自动扩大缓存掩盖不足或擅自换产品路线。

### POC 交付

交付测试包、命令、完整版本清单、测试样本说明和结构化结果。无法访问目标 Windows/GPU 时可以完成脚本、模拟测试与打包，但 POC 状态必须为“目标机待验证”，不能作为正式产品改造前的性能门槛已通过。

## 5. 里程碑 2：轻量 Runtime 与 GPU ownership

新增 `local_runtime/`，按配置/路径、后台宿主、阶段管理、进程生命周期和播放衔接分职责。复用现有模块，不新增复杂队列或分布式调度框架。

### 宿主与入口

- 一个 Windows 后台宿主承载现有轻量 HTTP 服务，共享同一个阶段管理对象；模型在各自受控子进程中运行。
- 将服务初始化提取为可调用函数，原 Mac CLI/脚本继续使用它们；不通过全局覆盖 argv 或大量 monkeypatch 拼装服务。
- 对 Studio 的扩展资源和合成块 manager 使用显式装配，保留所有现有功能。
- 桌面 Launcher 打开网页并显示后台状态；关闭浏览器不停止任务，显式退出应用才关闭受控后台及子进程。
- 单实例启动；默认业务端口保持 8765、8766、8000、8770、8771，均为回环监听。冲突时报错，不能终止其他程序。

### 状态与所有权

```text
IDLE → ASR → IDLE
IDLE → REWRITE → IDLE
IDLE → TTS_PREPARING → LIVE → STOPPING → IDLE
任一阶段出现错误 → ERROR
```

- 独立试听使用 `TTS_PREPARING`＋`operation=preview`，完成并释放后回到 IDLE；直播暂停仍处于 LIVE。
- GPU owner 与页面状态分开记录。只有确认上一模型卸载/退出后才清除 owner。
- 一个操作完成后卸载对应模型；改写的同一批任务内可复用模型，进入其他阶段前必须释放。
- 原子检查并获取所有权；禁止依赖每个服务各自一把锁。
- 包括 ASR 上传任务、泛化、事实润色、试听、直接 `/speak`/`/synthesize` 在内，所有单机推理入口都经过统一守卫；忙碌时返回 409 和当前占用任务。
- LIVE 期间禁止 ASR、泛化和独立试听；项目查询、查看/编辑副本不加载模型，不修改正在播放的确认稿快照。
- Runtime 只终止自己启动并持有句柄的进程。Windows Job Object 或等效受控进程树管理确保后台异常退出后不会遗留本程序推理子进程。
- 释放证据以受控进程退出、Ollama 模型卸载确认等为准。桌面与 OBS 仍可能占显存，不要求整张显卡用量归零；不能因暂时查不到显存数字就报告释放成功。
- ERROR 保留尚存活进程与 owner；提供明确停止/重试入口。未释放前拒绝下一模型，不能将错误状态简单清空。
- 复用 Mac 任务修复后的 ManagedEngine；停止或关闭失败时不得丢失进程句柄。

## 6. 里程碑 3：ASR、离线与在线 provider

### ASR 产品接入

- Mac MLX 实现保持，Windows 显式选择 CUDA worker；平台检查、模型验证和错误信息分别实现。
- ASR worker 通过结构化消息返回进度、chunk 结果、最终文本和错误，父进程负责现有结果持久化与 Studio 接续。
- 失败保留诊断与已完成结果；重启后不伪装成完成，不自动恢复播放。
- 模型预检查与真实推理成功分开展示，`asr_ready` 不能代表真实质量验收。

### Provider 与接口

- `provider` 增加 `ollama`、`openai_compatible`，保留 codex/chatgpt/gemini/agy。
- 本地默认 Qwen3 8B；离线模型列表从本程序实例读取，只显示已注册本地模型。
- 新建 Windows 项目默认本地 provider；旧项目保留其 provider/model，不静默切换。
- 本地前后端均按逐单元处理和逐段保存实现；事实润色等其他 `/api/generalize` 调用者同样适用长度预算。
- 在线 V1 支持一个连接配置：Base URL、Model、API Key；模型名可手填，不强依赖服务提供 `/models`。
- Base URL 是 API 前缀，例如以 `/v1` 结尾；统一追加 `/chat/completions`，避免重复 `/v1` 或重复 endpoint。
- 非流式请求包含 model/messages，读取 `choices[0].message.content`，继续执行现有 JSON/事实校验。错误、超时、401/429、空回复和截断均明确报告。
- API Key 只在后台使用；Windows 使用当前用户 DPAPI 保存。配置读取只返回是否设置，不回传密钥；项目、日志、导出和 Git 均不包含密钥。
- CLI provider 仍按原方式显式配置，Windows `.cmd/.exe` 路径和空格参数需实测；不假定所有旧 CLI 在新机器上已安装或登录。

### 新增/扩展的应用接口

| 接口 | 最小职责 |
| --- | --- |
| `/api/generalize` | 两个新 provider、现有候选结构、统一资源守卫 |
| `/api/models` | 本地已安装模型和在线配置模型 |
| `GET/POST /api/settings/models` | 外部模型路径、在线连接设置；读取不泄露密钥 |
| `/api/runtime/status` | state、gpu_owner、operation、active_model、pid、started_at、model_released、last_error |
| `/api/live/status` | 保留旧字段；增加 playback_state、generation_state、buffer_seconds、当前/失败序号、RTF 和缓存统计 |

所有本地写接口及更新接口校验应用会话和同源访问，保持现有内部鉴权，不把新单机入口开放到局域网。公开健康输出不包含凭证或不必要的本机路径。

## 7. 里程碑 4：本机直播、播放与缓存

### 播放链路

继续使用 AudioCache、AudioClient 的下载/ACK 语义、PlaybackController、WinMMPlayer 和 WAV 兼容层，由后台宿主接入。ACK 只表示传输落盘完成，不能当成已经播放。

单机增加严格播放模式，旧独立客户端的配置/协议保持兼容。Runtime 显式指定 `session_id`，不根据“最近下载文件”切换会话；本机状态上报也使用相同显式 Session。

### 操作契约

| 操作 | 行为 |
| --- | --- |
| 开始 | 固定当前最终稿快照和音色设置，申请 GPU，加载 TTS，绑定新 Session，生成到启动门槛后自动播放 |
| 暂停 | 暂停播放消费和下一块领取；当前合成可结束并留在本会话缓存；保留播放位置和 TTS owner |
| 继续 | 从同一播放位置继续，恢复生成；若处于真正耗尽后的缓冲状态，遵循补足缓存门槛 |
| 停止 | 立即停止播放，进入 STOPPING，禁止下一块，等待当前合成结束并释放 TTS 后回 IDLE |
| 再次开始 | 仅在已释放后创建新 Session，从其第一项播放；旧会话文件保留 |
| 退出/重启 | 不自动开播；保留稿件和缓存，恢复到可检查的空闲/中断状态；不借旧缓存自动续播 |

停止默认等待上限 120 秒；超时进入 ERROR 并保留未释放 owner，提供停止受控引擎入口。确认退出前禁止新会话，不要求强行中断 GPU kernel。

### 顺序、失败与缓存

- 同一 Session 的 sequence 单调递增，维护期望序号；缺失、失败项阻止越过它播放后续内容。
- 播放失败进入错误状态，记录 failed_sequence；用户重试同一项成功后再推进，保留既有 PCM16 兼容恢复的一次性限制。
- TTS 失败也不得跳到下一段或无限自动重试；显示失败源段，显式重试恢复该段，或停止会话。
- Strict 模式禁用旧会话自动物理清理；排除旧内容靠 Session/状态，不靠删除文件。
- 默认启动缓存 30 秒、低水位 30 秒、高水位 90 秒，配置化并校验 `0 < startup <= high`、`0 <= low < high`。
- 缓存时长以调速处理后、本机可播放的 WAV 为准，包含当前播放剩余时间；下载完成但 ACK 未确认的文件不能提前算作可消费项。
- 耗尽进入 buffering，补足启动门槛再继续；有限单轮若总长度不足门槛且生成完成，有音频时立即播放，空结果报错。
- 高水位暂停领取下一块，低水位恢复；允许一个在途块造成有限超量，禁止无界堆积。
- 统计未缓存模型 RTF、端到端供给 RTF、P95 单块准备耗时、命中/未命中、缓冲次数和错误。低水位是否覆盖延迟由实测决定。
- 动态时间继续在提交 TTS 前替换，合成缓存键使用替换后的文本；不要把预缓存音频宣称为实际播出时刻的精确报时。

试听也使用现有 Windows 播放底层，避免调用 afplay。Mac 本机播放和现有 LAN 传输模式保持原语义。

## 8. 里程碑 5：安装、开发更新与正式更新

### 交付物

1. `AI-Live-Studio-Setup.exe`：应用、Web、Launcher、Runtime、ASR 专用 Python 环境、CosyVoice 及匹配运行库、Ollama standalone、FFmpeg、播放模块、配置与更新组件。
2. `AI-Live-Studio-Offline-Models-V1`：模型文件、离线导入材料、版本/hash 清单和固定音色资源；私人稿件/录音不进入通用安装包。
3. 文档、版本清单、校验文件及开发更新脚本。

沿用 PyInstaller＋Inno Setup，程序采用便于分离运行库的目录构建，不把数 GB 模型嵌进单个 EXE。使用新的固定 AppId，不覆盖旧 AI Audio Client 产品身份。

用户不需要手动安装 Python、CUDA Toolkit、Ollama、FFmpeg；NVIDIA Driver 由用户系统提供。运行库按组件隔离查找路径，锁定兼容版本，避免多个 CUDA DLL 目录混用。不要把 POC 环境中碰巧已装的软件当成产品依赖已经随包交付。

### 更新最小实现

- 用一个更新组件支持开发/正式渠道；开发脚本 `scripts/windows-update-dev.ps1` 调用同一清单与校验规则，不另写一套 updater。
- 业务只读取配置化的 HTTPS 版本清单；GitHub Release 由薄适配层提供，未来可换 HTTPS 服务器。没有真实更新源时明确未配置，用本地测试服务器/fixture 验证流程，不虚构发布地址。
- 清单包括 version、release_notes、installer_url、sha256、mandatory、运行时/模型兼容范围；当前版本取已安装版本，不由服务端伪造。
- `/api/update/status`、`/api/update/check`、`/api/update/apply` 只处理已核验的候选版本；不接受任意命令或任意安装包 URL 执行。
- 校验 HTTPS 来源与 SHA256；正式分发增加发布者签名核验，不能把 hash 单独当作发布者身份证明。不得为测试放宽正式渠道校验。
- 离线、检查失败或更新源未配置不影响正常离线功能，不自动调用在线 provider。
- 下载可在后台进行；apply 仅 IDLE 且所有受控模型/任务已结束时允许。LIVE、STOPPING、ASR、REWRITE 等返回 409。
- mandatory 不打断正在直播的会话；只能约束下一次启动，且不得因网络检查失败凭空生成强制升级状态。已知强制更新可通过离线安装包完成。
- 用户确认应用更新后，保存状态、停止受控后台、由独立 updater 启动安装器。Program Files 升级使用系统提权，业务程序重启到原用户身份。
- 使用安装器替换应用，不原地覆盖正在加载的 DLL。安装退出码成功后才记录新版本；失败/取消保留旧版本信息及恢复安装包，提供明确修复/恢复步骤。
- V1 不引入破坏性项目格式迁移；恢复旧程序版本仍可读取用户数据。程序、模型清单不匹配时提示兼容问题，不自动下载模型。
- 更新、卸载默认保留全部用户数据和外部模型。下载包、临时文件的清理由明确清理操作管理，不因任务完成自动删除用户文件。

## 9. 验收矩阵

| 类别 | 必测场景与通过条件 |
| --- | --- |
| GPU 交接 | ASR→LLM→TTS 多轮切换；上一模型退出/卸载有证据，错误时 owner 不提前清空 |
| 并发入口 | 同时点击/直接调用 API；仅一个任务取得资源，其他返回 409；查询不启动模型 |
| 模型与目录 | 中文/空格路径、没有 D 盘、缺权重、GGUF 未注册、版本不兼容；都给出可行动错误 |
| 完全离线 | 断网完成导入录音、转写、改写、确认、TTS、播放；无隐式拉模型或云端 fallback |
| ASR | 静音/BGM/无停顿/长录音/分块边界数字与重复话术，完成或失败后 worker 退出 |
| LLM | 超上下文、候选缺失、错误 JSON、取消/失败后的逐段保存、事实改写核对 |
| 在线配置 | 假兼容服务覆盖成功、401、429、超时、空输出、截断；日志/导出无 Key；真实服务未配置时标明未做真实调用 |
| 播放 | 暂停位置、继续、停止中再开始、旧 Session 晚到、缺序号、播放失败、TTS 失败、缓冲耗尽、短单轮、重启不自动开播 |
| 性能 | 目标 12400/3060 Ti/32GB 同机普通 1080p 推流至少 2 小时；报告编码器、帧率、语速和样本，不以缓存命中充当 GPU 性能 |
| 持续运行 | 无持续缓存耗尽、OOM、引擎崩溃或前一阶段进程残留；未缓存供给 RTF ≤0.67 为目标门槛 |
| 安装更新 | 干净普通用户安装、提权取消、损坏包、错误 hash/签名、直播时 apply、失败恢复、升级后数据/模型/音色保留 |
| Mac 回归 | 当前入口、项目/事实处理、MLX ASR、TTS、CLI、LAN 缓存和旧客户端模式的相关测试通过 |

常规 CI 运行 fake 后端、临时端口和播放器测试；Windows CI 构建完整产物。真实 Windows GPU、真实声音、OBS 两小时必须单列证据；普通 windows-latest 不自动具备这些条件。

## 10. 里程碑交付与完成定义

每个里程碑提交代码、相关回归测试、运行说明和已验证/待验证清单，不把测试和文档全部推到最后。

1. GPU POC＋可运行测试包和实测报告。
2. Runtime 状态机、资源守卫、释放确认、生命周期回归。
3. Windows ASR、Ollama、通用在线 API、DPAPI、设置与项目兼容。
4. 严格 Session 播放、失败屏障、统一操作、30/30/90 缓冲与性能统计。
5. 安装器、开发/正式更新、数据保留、Mac 回归、目标机长时间验收。

执行智能体可以自行完成必要的常规实现选择；涉及新增产品范围、改变 Mac 受保护行为、发布或账号权限时按已有授权处理。任务难点或缺少目标 GPU 时，继续完成可独立验证的工作，并准确标明未达成门槛。

不做多 GPU、多路直播、并行驻留、复杂任务队列、云端 Runtime、自动模型下载、模型增量更新、P2P 或复杂自动恢复系统。

最终交付必须包含：分支与基线 SHA、提交列表、安装包/模型包位置、版本/hash 清单、目标机报告、Mac 回归结果、更新验证结果和全部剩余限制。没有目标硬件实测时不得宣称“3060 Ti 上完整实时直播已通过”。

## 11. 已核对的上游参考

- [Qwen3-ASR 官方实现与本地推理](https://github.com/QwenLM/Qwen3-ASR)
- [CosyVoice.cpp Windows/CUDA 构建与后端说明](https://github.com/Lourdle/cosyvoice.cpp)
- [Ollama Windows standalone 分发](https://docs.ollama.com/windows)
- [Ollama GGUF 离线导入](https://docs.ollama.com/import)
- [Ollama chat 与卸载参数](https://docs.ollama.com/api/chat)
- [Ollama 已加载模型查询](https://docs.ollama.com/api/ps)
- [OpenAI Docs：Chat Completions 请求与响应](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)
- [Windows 安装包签名验证](https://learn.microsoft.com/en-us/windows/win32/seccrypto/using-signtool-to-verify-a-file-signature)

以上支持实现路线，不替代锁定依赖版本和目标硬件测试。
