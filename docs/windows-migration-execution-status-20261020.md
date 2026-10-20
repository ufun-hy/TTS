# Windows 单机化迁移计划：执行状态与阶段 0 基线交接

更新：2026-10-20
目标分支：`codex/windows-offline-runtime-v1`
原始任务：`Windows 单机化重构：模块盘点与迁移计划.md`

## 当前执行状态

本任务按原始文档定义的顺序执行，不以此前桌面壳、音频库存等旁路功能的提交代替阶段 1–5 的架构迁移。

| 阶段 | 本次判定 | 证据 / 下一道关 |
|---|---|---|
| 阶段 0：测试安全网 | **功能回归 PASS；安装包构建待完成** | Windows 双版本功能回归已绿；全量 Windows runtime tests、WinMM smoke 和 dry-run 在安装包工作流中已通过，打包步骤仍在执行 |
| 阶段 1：砍 Mac/局域网遗留 | **两批清理已提交，仍在进行** | 删除 macOS `server/stack_services.py`、对应 Python CLI 和两个 launchd 专项测试；其余脚本及旧网络链路待审查。`audio_cache` 保留到阶段 4 |
| 阶段 2：移植 main 六个业务模块 | 尚未执行 | 必须逐项迁移并跑对应测试，不能直接覆盖 Windows 交叉修改 |
| 阶段 3：安全修复 | 尚未执行 | Origin/Host/Content-Type，TTS gateway 异常及认证，离线补丁签名 |
| 阶段 4：单宿主进程化 | 尚未执行 | 新 Host、Windows Job Object、GPU 调度、进程内队列；需实机验证 |
| 阶段 5：文档/分支收尾 | 尚未执行 | 所有阶段通过后才能进行 |

## 已提交的阶段 0 修改

- `tests/test_wav_compat.py`：测试中的 monkey-patch `changing_stat(path, *args, **kwargs)`，传递 Python 3.13 新增或扩展的 stat 关键字参数，避免误报类型错误。
- `.github/workflows/windows-functional-regression.yml`：Windows Python 3.11 / 3.13 矩阵；覆盖本阶段已有桌面、库存、回放测试，并加入 `tests.test_wav_compat`。
- Windows Functional Regression run `39363238597`：Python 3.11 / 3.13 新增测试通过，既有测试均因未准备 FFmpeg（两项）及 Windows UTF-8 默认编码断言（一项）失败。\n- 针对性修复：`tests/test_text_studio.py` 明确 `read_text(encoding="utf-8")`；功能回归工作流按已有打包工作流的 SHA-256 固定版本安装 FFmpeg。\n- **后续复测 run `39363739503`：Python 3.11 与 3.13 两个作业均 SUCCESS。** https://github.com/ufun-hy/TTS/actions/runs/39363739503\n- Windows Installer run `39363716583`：截至本次核查，测试、WinMM smoke、Runtime dry-run 已成功，`Build full installer` 仍在执行，整条流水线尚未结论。https://github.com/ufun-hy/TTS/actions/runs/39363716583

## 阶段 1 第一批变更

在回归基线 Python 3.11 / 3.13 两套功能检查通过后，按原计划先删除 macOS launchd 专属代码：
- `server/stack_services.py`（launchd 生命周期）
- `scripts/stack-services.py`（仅调用上述管理模块的旧 CLI）
- `tests/test_stack_services.py`（针对该 Mac 管理模块的测试）
- `tests/test_stack_launchd_integration.py`（macOS launchd 集成测试）

本批**不涉及** Windows 播放器、Audio Cache、`scripts/windows-runtime.py`、`recording_transcript/windows_worker.py` 或 `main`。尚存的 macOS Bash 脚本、Mac Keychain 调用、MLX 分支、旧 timeline CLI 以及局域网轮询链路仍应按原计划继续分批审查，不得宣称阶段 1 全部完成。

删除后的 CI 工作流 run `39364041752`、安装包 run `39364041732` 已触发，写入报告时还未确认最终结果。

## 2026-10-20 阶段 1 第二批清理（Windows 目标分支）

按原文档第 3 节与第 6 节，继续清理已确认的 macOS 专用运维入口，分两次独立提交：

1. Commit `eb7445c617d42bbbc5dc93cc09f2218d490b7874`：删除 15 个 macOS 运维 Shell，包含 `scripts/funnel-*.sh`、`scripts/service-*.sh`、`scripts/stack-{common,start,stop,restart,status}.sh`、`scripts/managed-service.sh`。这些脚本依赖 launchd、Keychain 或 Tailscale Funnel，Windows Runtime 仍走 `scripts/windows-runtime.py`。
2. Commit `d89427deb20511f655407cf73b92aa75f6d1e291`：删除 `scripts/timeline-{run,generalize,resegment}.py` 三个旧 CLI 入口。**尚未删除 timeline 的算法和 `tts_client.py`**，因为 Windows 服务仍有运行时调用；它们必须在依赖迁移验证后才能继续移除。

保持不动：
- `audio_cache/*`、`audio_client/playback.py`、`audio_client/wav_compat.py`：旧播放路径需要等阶段 4 替换；
- `recording_transcript/windows_worker.py` 和 Windows 专属启动、安装、升级模块；
- `start.sh`、`scripts/text-studio-start.sh`、`scripts/recording-transcript-start.sh` 暂留待依赖审查，不能因为是 Shell 立即全部删除；
- `main`、模型文件及用户运行数据。

CI 状态（本批操作时的观察）：
- 删除 15 个 Mac Shell 后的 Windows Patch workflow run `39366049658` 已 **SUCCESS**；
- Installer run `39366049653` 当时仍在执行；
- 再删除 3 个 timeline CLI 后 Patch run `39366121165` 与 Installer run `39366121166` 已触发，**未取得最终结果**；
- 前一批 `launchd` 删除后的功能回归 run `39364041752`、Patch run `39364041721`、Installer run `39364041732` 均已 **SUCCESS**。

**阶段 1 仍未完成**：timeline 其余内部调用清理、`/speak` 与 `afplay` 清理、剩余 Mac 入口清理、最终导入/打包回归。

## 阶段 1 第三批：TTSClient 与 ASR Windows 专用化

- 新增 `server/tts_client.py`，保留原 `/synthesize` HTTP 客户端行为，API Key 仅由 Runtime 环境变量或显式参数提供，不再调用 macOS Keychain。
- `server/live_session.py` 和保留的 `timeline/scheduler.py` 均改从 `server.tts_client` 导入；删除 `timeline/tts_client.py`。
- `recording_transcript/qwen_asr.py` 改为纯 Windows CUDA 后端兼容接口，移除 MLX 加载与 macOS 自动选择分支；底层保留 `recording_transcript/qwen_asr_windows.py` 及 `windows_worker.py` 子进程执行。
- `voice_datasets/transcription.py` 中 ASR 版本来源从 `mlx-audio` 改为 `qwen-asr`，backend 记录使用 CUDA 常量；`recording_transcript/pipeline.py` 更新误导性 MLX 描述。
- 新增 `tests/test_tts_client_migration.py`，更新 `tests/test_qwen_asr.py`，并将 TTSClient、ASR 和声音数据集测试纳入 Windows 功能回归工作流。
- 该批当前 GitHub 工作流 run `39366829015` 已触发；最终状态需要以 Actions 实际执行结果为准。

**重要兼容性边界**：已有历史声音数据集若采用 `mlx-audio` 标识，其 identity 不会自动改写；Windows 新运行会使用 CUDA 标识，旧目录如出现 identity mismatch，须保留旧目录并创建新任务目录，不得静默覆盖历史结果。

尚未完成：`/speak`+ `afplay`、剩余 Mac shell（包括 `start.sh`）、timeline 其余算法模块的引用清理、其他潜在 MLX 依赖与旧 audio_client 轮询协议；最终完成标准是功能/安装包测试绿色且不存在 Mac 专属调用。

### 第三批 CI 复测与问题修复

初次 CUDA 迁移 CI run `39366829015` 在 Python 3.13 报错，定位到：
- `audio_cache/server.py` 仍导入已删除的 `timeline.tts_client`；
- `recording_transcript/windows_chunks.py` 依赖 Python 3.13 不再提供的 `audioop`。

已针对性补救：
- Audio Cache 的 TTSClient 导入统一指向 `server.tts_client`；
- 用整数 PCM RMS 替换 `audioop.rms`，保留 16/32-bit PCM 的阈值比较行为；
- 增加 `tests/test_windows_asr_chunks.py` 并纳入 3.11/3.13 CI。

补救后最新 CI run `39366991723` 已触发，写入本记录时尚未取得最终结论。**不得将初次失败的批次标记为 PASS。**

## 阶段 1 第四批：移除 TTS Gateway 的 Mac 现场播报入口

- `server/tts_gateway.py`：不再接受 `POST /speak`；仅保留 Windows 客户端使用的 `POST /synthesize`。
- 清除 `/usr/bin/afplay` 调用；保留现有 `/synthesize` 合成及认证路径，`/speak` 请求由网关返回 404。
- 将 Gateway 启动日志里的 API 地址改成 `/synthesize`。
- `tests/test_stage1_efficiency.py` 增加旧 `/speak` 请求返回 404 的回归检查。
- 为避免提前改变旧系统内部的 worker 生命期和 `/health` 协议，本轮**没有**删除 Gateway 的内部 Queue/Worker 类；该内部结构需要阶段 4 的单宿主队列替换后清理。
- 变更提交：`0aafc08374e8c76e19e85ca20d2084319492ef4a`，测试提交 `7f30105ee2fc74790fb9de8974edf505532132bb`。
- GitHub Windows Functional Regression run `39367307297`（首个业务变更）已 SUCCESS；包含新增旧路由断言的 run `39367347746` 已启动，最终结果待核对；Installer/patch 运行中。

**阶段 1 仍待**：剩余 Mac Shell 脚本的测试/打包引用收敛、timeline 历史算法与测试依赖界定，完整的项目级引用排查与最终 Windows 安装包验证。**不在本阶段替换 Audio Cache 和现有 WinMM 播放路径。**

## 阶段 1 第五批：Mac 安装/启动入口清理

- 删除 6 个仅针对 Mac 或 Apple Silicon 的 Shell 入口：根目录 `start.sh`，`scripts/install.sh`、`scripts/cosyvoice-runtime-ab.sh`、`scripts/voice-prepare.sh`、`scripts/text-studio-start.sh`、`scripts/recording-transcript-start.sh`。删除提交 `1689c51fd1fb6ccc70f7e6400e4fb5380448dc7d`。
- `tests/test_service_entrypoints.py` 仅删除了 macOS Bash 专属检查；保留 Windows 可用的 Python entrypoint 和直接启动测试。提交 `445b3a033d668b8b68c3901316dcbe1d1af7dacf`。
- 仍保留 `timeline/engine.py`、`timeline/scheduler.py`、`timeline/resegmenter.py` 等算法模块：当前测试直接导入相关逻辑，不能因删除 CLI 就认定全目录可删除。阶段 1 本批不动算法，以免丢失现有功能。
- 扩展 Windows 功能回归矩阵：包含 Python entrypoint、timeline runtime/generalization/resegmentation/sentence reconstruction 的测试，变更 `2fef8614a837a461afe0b0d280331737d2ab2d18`。
- 完整安装包和补丁构建由 Shell 删除触发；最终 CI 需要查看对应 workflow runs，写入本报告时尚未确认结果。
- Windows Runtime 仍按 `scripts/windows-runtime.py` 启动，Windows 安装包由 `build/windows/package_app.py` 读取 Git tracked 文件；已删除文件不会纳入新构建的分发清单。

**阶段 1 出口尚未完成**：仍需对项目各处的旧 Mac/MLX/局域网依赖进行全量引用检查，核验 Windows 最新 Installer/Patch，确认哪些 timeline 模块能在不丢失功能的前提下迁移或清理，不能宣称阶段 1 全部 PASS。

## 阶段 1 出口核查与阶段 2 首批移植

阶段 1 最新复核：
- Windows 功能回归 run `39367670323`：Python 3.11、3.13 **SUCCESS**。
- Windows Patch run `39367640187`：**SUCCESS**。
- Windows Installer run `39367640270`：检测时 Windows Runtime tests、WinMM smoke、runtime dry-run 步骤 **SUCCESS**，完整 Installer 构建尚未取得最终结论。完整阶段 1 出口和 Windows 实机验证因此不能标记全部 PASS。
- `timeline` 的运行时调度/选择/文本生成算法仍被专项测试导入，暂时保留；Mac shell 运维与入口已按批次移除。
- Git 比较显示 Windows 分支比 `main` 落后 **24 个提交**；这些更改涉及业务和播放协议的交叉修改，禁止直接覆盖或整体合并。

阶段 2 第一批（独立模块移植，**未接入 UI/Live Session**）：
- 已从 `main` 原样引入 `config/text-studio-prohibited.json`、`server/prohibited_speech.py`、`server/semantic_tts_blocks.py`、`server/context_variants.py`。
- 已同步引入 `tests/test_semantic_tts_blocks.py`、`tests/test_context_variants.py`；新增 Windows 独立禁用话术测试 `tests/test_migrated_prohibited_core.py`，并将三者纳入双 Python 矩阵。
- 上述模块构成可独立测试的基础，**尚不能宣称完整的禁用话术管理、上下文候选生成及语义播报 UI 已迁移**。
- `main` 的 `first_round_preparation` 等模块仍引用旧 `timeline.tts_client.RetryableTTSClientError`，而 Windows 分支已迁入 `server.tts_client`；后续须先解决契约差异，再接入预生成与直播。
- 主干 `server/text_studio.py`、`server/live_session.py` 和 `web/text-studio.html` 与 Windows 现状冲突，不能照搬，需要按 API 功能单独合并和回归。

剩余阶段 2 迁移顺序建议：先完成各独立模块专项测试；再合并前端交互与 Studio API；最后接入第一轮计划/预生成、恢复逻辑，逐项验收。

## 阶段 0 未完成条件

1. GitHub Actions 真实运行并得到 Python 3.11 / 3.13 两套测试结论。
2. 完整既有 Windows Runtime 测试套件结果与失败清单。
3. Windows 实机安装包/本地环境的功能烟测。GPU/WinMM 不要求在云端 runner 假装通过。
4. 记录稳定基线 Commit 及可复现的运行环境。
5. 若测试失败，仅修复确认属于兼容和回归的问题，不跳过测试宣布 PASS。

## 严格顺序与安全约束

阶段 0 通过前，不大规模删除旧 Mac/网络模块，不迁移运行时入口或更换 Audio Cache，更不修改 main；避免将功能回归和大规模迁移交织在一起。

后续阶段 1 删除前须逐条核对导入、脚本执行入口和打包清单。尤其 `recording_transcript/windows_worker.py` 是动态子进程入口不可删除，`audio_cache/processing.py` 的音频变速/增益处理必须保留。

## 结论

**真实进度：阶段 0 的 Windows Python 功能回归已 PASS；安装包构建与真实设备烟测仍待验收，未将整个阶段标记完全完成。** 之前的桌面壳与库存功能只代表另外一批代码改造，不能视为“阶段 4 单宿主已完成”。

## 阶段 2：禁用话术 Windows 端到端接入（2026-10-20）

本批仅实现阶段 2 第一项；`main` 原有的完整风险抽屉/候选组设计没有整体覆盖 Windows 页面，以保留单机化的音频生成与播放解耦。

已提交：
- `server/text_studio.py` 按主干规则校验 `prohibited_reviews`，项目 JSON 继续原样持久化；试听在调用 CosyVoice3 前执行 `broadcast_text`，全禁用时失败且不产生 WAV；`/api/generation/start` 与兼容的 `/api/live/start` 在 `live.start` 前用 `filter_segments` 过滤，并在无可播内容时拒绝开始。
- Text Studio 为浏览器提供 `/text-studio-prohibited.js` 和与服务端同源的 `/text-studio-prohibited-rules.js`；前端显示每段命中数、允许逐条确认或定位修改，人工确认使用候选完整文本、命中位置、标签、规则版本与确认时间绑定；提供撤销该段确认能力。
- 项目保存/加载包括 `prohibited_reviews`；编辑或切换候选清除该段过期确认；客户端向试听和生成接口传输确认记录。所有确认继续由服务端按完整文本/位置/规则版本再校验，不依赖前端显示状态。
- 新增 `tests/test_windows_prohibited_integration.py`、`tests/windows_prohibited_ui.js`；新增 Python 双版本测试、前端 JS 语法和浏览器规则验证。
- Windows CI run `39369362620`（新增端到端测试及前端校验步骤）Python 3.11/3.13 均 **SUCCESS**。随后 Unicode 位置兼容性和撤销按钮补丁已提交，最新 CI 仍需确认结束结果。

局限及验收要求：
- 前端实现的是轻量逐条确认/编辑体验，没有完全复制 `main` 风险抽屉的分类与上下文候选组交互；阶段 2 其他模块仍未接入。
- 当前支持的是**每段最终选中候选**的确认与过滤，未加入上下文组批量确认。
- 需要在 Windows 真实工作台依次验收：加载项目→命中→人工确认→保存重启→试听→生成→库存 WAV→播放器播放，并验证不含未确认禁用文本；最新 Installer 完整构建和实机操作结果尚未取得。
- 本次不修改播放组件、不清理用户已有数据、不部署。
