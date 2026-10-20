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

**阶段 1 仍未完成**：timeline 内部调用迁移、`timeline/tts_client.py` Keychain 剥离、MLX ASR 拆分、`/speak` 与 `afplay` 清理、剩余 Mac 入口清理、最终导入/打包回归。

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
