# 手动启动、持续运行的本地服务

继续使用：

```bash
./scripts/stack-start.sh
./scripts/stack-status.sh
./scripts/stack-stop.sh
./scripts/stack-restart.sh
```

`stack-start` / `stack-restart` 需在已登录的 macOS 用户会话中执行。音频缓存、Text Studio 和录音转文稿由当前用户的 launchd 管理；启动命令或终端退出后，服务继续运行。三个任务的 plist 写入 `runtime/launchd/`，不安装到 `~/Library/LaunchAgents/`，因此它们不会在下次登录时自动启动。原有 `com.ufun.tts` 的安装及登录行为不变。

三个任务分别为 `com.ufun.tts.audio-cache`、`com.ufun.tts.text-studio`、`com.ufun.tts.recording-transcript`，端口保持 8000、8770、8771。重复启动健康任务不会重建进程。显式停止使用 `launchctl bootout`，停止后不会自动恢复；异常退出则由 launchd 恢复。入口每次执行前等待 10 秒，确保即使进程运行很久后退出，也至少等待 10 秒才重新启动业务进程。首次启动每项也有此等待，单服务健康检查上限为 45 秒。

`server/stack_services.py` 维护任务配置、归属校验、迁移、状态和生命周期；`scripts/stack-services.py` 是命令入口；`scripts/managed-service.sh` 负责服务环境与凭证读取，随后 exec 现有业务入口。Python 使用启动时解析的绝对路径；录音服务保持使用 `runtime/qwen-asr-venv/bin/python`，可通过 `RECORDING_TRANSCRIPT_PYTHON` 明确覆盖。PATH、工作目录、项目导入路径固定到配置中，Text Studio 独立入口也不再依赖调用者的 PYTHONPATH。

## 认证与旧进程迁移

启动和重启在停止任何进程前检查凭证。TTS 密钥由服务入口读取现有 Keychain 项 `com.ufun.tts.api-key`，支持原有 `TTS_KEYCHAIN_SERVICE` 名称设置；密钥不会写入 plist、命令行或日志。每次启动会排除 launchd 全局环境中的旧密钥。

当前托管模式采用现有 Keychain TTS 凭证及未设置额外缓存密钥的配置。若调用环境或 launchd 环境存在 `AUDIO_CACHE_API_KEY`，或现有 Audio Cache 返回鉴权要求，则拒绝迁移，保留已有服务。自定义 `TTS_API_KEY` 与 Keychain 值不一致时同样拒绝。不会为了启动而关闭鉴权，也不自动创建或持久化新凭证。仍需使用自定义缓存密钥时，保持原有显式环境的前台启动方式，直至另行配置持久凭证支持。

首次托管启动仅迁移完整脚本路径属于当前项目的旧进程，包括旧版 `server/text_studio.py`。陌生端口监听者或属于另一 checkout 的 launchd 任务不会被终止、覆盖。旧 PID 文件只作为线索，每次都会重新核对进程身份，不自动删除或更新它们。此后以 launchd 的 PID 为准。

## 故障诊断

状态命令同时显示 launchd 状态、当前 PID、最近退出码/信号、监听端口和 HTTP 健康结果。READY 要求任务属于当前项目、进程存在且对应端口由该 PID 监听，并且健康接口成功。单服务启动失败返回非零状态，但仍尝试启动其他辅助服务，不停止已经健康的服务。

若网关在线但试听返回 `tts_failed`，应检查 `cosyvoice-server.log` 并实际执行一次试听。macOS 系统 Python 可能清除 shell 传入的 `DYLD_LIBRARY_PATH`；引擎启动器会在每次启动原生 CosyVoice 子进程时显式补入引擎所在目录及 ICU 库路径。仅网关处于 sleeping/ready 状态不能替代实际合成验证。

业务日志继续追加到 `runtime/logs/audio-cache.log`、`text-studio.log`、`recording-transcript.log`。启动请求和实际启动时间均有记录；`stack-lifecycle.log` 追加管理操作、超时和失败信息。最近退出状态由 launchd 提供，显式停止前也会记录到生命周期日志。日志、配置、测试目录、项目数据和旧 PID 文件均不自动清理。

验证命令：

```bash
python3 -m unittest discover -s tests -p test_service_entrypoints.py
python3 -m unittest discover -s tests -p test_stack_services.py
RUN_LAUNCHD_TESTS=1 python3 -m unittest discover -s tests -p test_stack_launchd_integration.py
python3 -m unittest discover -s tests -p test_text_studio.py
node tests/test_text_studio_risk.js
```

真实 launchd 集成测试使用唯一任务标签、临时目录与独立回环端口，覆盖启动器退出、重复启动、异常恢复、至少 10 秒恢复间隔、显式停止、日志追加和旧 PID 文件保留；不会操作正式服务或读取 Keychain。测试后卸载测试任务，保留测试文件。
