# Windows 转写任务与关闭恢复

2026-09-29 修复，基于 Windows 分支 `d2779ec`。用户数据位于
`%LOCALAPPDATA%\AI-Live-Studio`，应用位于 `C:\Program Files\AI Live Studio`。

## 已修复行为

- 启动时识别旧 PID 已被其他可执行程序复用的情况，忘记过期归属记录，不终止其他程序。
- 上传显示字节进度；解码、模型加载独立显示；识别显示已完成音频秒数、分段数和百分比。
- 暂停终止本次 ASR 子进程，保留分段检查点；继续会重新加载模型，跳过完成的片段，重做中断片段。
- 任务元数据、上传副本、解码 WAV 和检查点保存到 `recording-transcript/jobs/<id>`；服务重启后未完成任务显示已暂停。
- 删除先终止任务，再将任务及结果移入 `recording-transcript/trash`，不会删除用户原录音；回收区不自动清理。
- 关闭转写页发送暂停请求，异常断线约 20 秒后也会暂停。
- Windows Studio/转写页分别持有页面会话。所有页面关闭后约 15 秒停止服务；断线、浏览器崩溃则在最后心跳约 90 秒失效后停止。首次启动未打开页面，5 分钟后停止。
- 多个软件页面存在时，关闭一个页面不会关闭其他页面对应的 Runtime；关闭转写页仍会暂停转写。
- ASR worker 持有父进程句柄并监控退出，父服务异常终止时 worker 自行退出，释放 GPU。

## 回收恢复

暂停任务直接点“继续”。删除任务可在软件停止后，将 `trash/<id>-<后缀>` 移回
`jobs/<id>`，其中 `result.json` 如存在则移回 `results/<id>.json`。
恢复前确认目标不存在，避免覆盖另一个任务。

## 本次目标机处理

目标 `192.168.137.7 / CHINAMI-LRAUI91`，用户 `EDY`。
旧 Ollama 记录 PID 6080 实际指向 `sihost.exe`；原文件已重命名为
`runtime/windows-processes.json.stale-20260929-172550.bak`。
首次文件更新备份：`updates/transcript-controls-20260929-173425`，包含旧源码、manifest 和失效的 GPU 标记。
旧版本未持久化的两个转写任务无法恢复识别断点；其原上传临时副本继续保留在 EDY 的 Temp 中。

## 验证

本地回归覆盖暂停/继续、重启恢复、可恢复删除、真实子进程终止和 PID 复用保护。
Windows 内置 Python 上执行相同核心回归；另在目标机 CUDA 上做真实音频验证。
本补丁仅修改应用源码/页面及受控运行记录，没有升级模型或 Python 依赖。

真机记录：130 秒音频完整转写通过；另一次在 60 秒（1/3 段）暂停，服务重启后继续到 130 秒（3/3 段）。测试任务删除后移入回收区。Windows 父进程被结束时，ASR 子进程退出的实际进程测试通过。

## 本地泛化与播放

两次 Qwen3 泛化失败的原始响应缺少 JSON 最外层结束括号。Ollama 泛化请求现使用 JSON Schema 约束段落 ID 和候选数量；不再把破损外层 JSON 中的内层数组误当完整结果。原失败的 p0002、p0015 在目标机各生成 3 个候选成功（6.7 秒）。这验证输出结构，不替代用户对事实和措辞的审阅。

更正：本机 Vulkan 设备顺序会变化，`Vulkan1` 不能固定代表 RTX3060。先前写入的
`config/tts-backend.json` 数字配置不可靠，新的本地修复已停止读取它。
新启动策略使用 `nvidia-vulkan` 身份策略：每次引擎唤醒时，只给 CosyVoice 子进程设置 NVIDIA 驱动过滤，使用同目录 GGML DLL 验证只有一张 NVIDIA 独显可见，再启动引擎。过滤无效、存在 Intel、无独显或多张独显无法唯一选择时均拒绝启动，不回退 CPU。
播放器应由 Windows 桌面启动器启动，不能把 SSH Session 0 的运行结果当成桌面声卡验收。

使用：Windows 浏览器打开 `http://127.0.0.1:8770/`，选音色，段落点“试听”；完整话术准备后进入“直播控制”→“开始循环智播”。试听由浏览器播放，循环智播由本机 WinMM 播放服务播放。输出使用 Windows 默认音频设备，先在 Windows 声音设置选择音箱或 USB 声卡。初次智播需积累缓存（默认 30 秒）后播放。

2026-09-29 19:15：桌面会话 Session 2 已运行播放服务；一次 `/api/tts/preview` 返回 HTTP 200、PCM16 单声道 24000Hz WAV，5.28 秒（253518 字节）。此证据仅证明生成了音频，不能证明在 RTX 上运行。后续日志确认设备顺序反转且固定 Vulkan1 选中 Intel，出现 GGML 断言。

2026-09-29 后续身份修复验证：桌面会话使用 NVIDIA 驱动过滤及显式驱动文件时，GGML 仅枚举到 RTX3060；SSH 提权会话的驱动过滤被忽略时，检测器会拒绝混合设备。仅用文件名过滤、不使用显式驱动路径的桌面验证仍待读取结果。23:21 两个网卡地址均失联，身份修复尚未部署，必须在目标机恢复在线后完成过滤与真实合成/GPU 使用验证。

## 2026-09-30 NVIDIA 身份修复部署

恢复连接后确认仅 `VK_LOADER_DRIVERS_SELECT=nv-vk64.json,nvidia*.json` 即可在 EDY 桌面会话过滤 Intel 驱动，无需固定 DriverStore 路径。已部署 `local_runtime/vulkan_device.py`、`server/engine_runtime.py` 和 `scripts/windows-runtime.py`；原文件与 manifest 备份至 `updates/nvidia-identity-20260930-100755`。旧 `tts-backend.json` 保留并已备份，但 Runtime 不再读取其数字索引。

每次 CosyVoice 冷唤醒先用同目录 GGML DLL 验证设备身份；子进程只看到唯一的 NVIDIA 独显。在这个过滤后的设备集合中，`Vulkan0` 是校验所得的别名，不是整机固定索引。日志记录 `NVIDIA_VULKAN_SELECTED`。过滤失败、无独显或多张独显无法唯一识别时拒绝启动。

10:08 真机证据：日志仅显示 RTX3060 一个 Vulkan 设备；`nvidia-smi` 列出 CosyVoice PID 13820，合成期间 GPU 利用率达到 100%，总显存从约 598 MiB 升到 2569 MiB。HTTP 返回 200，生成 12.48 秒 PCM WAV（599118 字节）。Windows WDDM 未提供单进程显存数值，不能把总显存全部归于 CosyVoice。合成结束后确认原引擎进程已退出。

本地设备策略、启动环境和打包相关 18 项测试通过；目标 Windows 内置 Python 的 3 项设备策略回归通过。此前包含其他功能的综合检查为 64 项、1 项平台跳过。

10:09 第二次冷唤醒再次仅识别 RTX3060，独立新文本合成完成（引擎 3.246 秒，241920 samples / 24000Hz，即 10.08 秒音频）。结束后显存回到约 652 MiB，CosyVoice 引擎进程已退出。部署的三个文件与本地 SHA256、目标 manifest 均一致。两个临时桌面启动/探测计划任务已移除，探测文件、音频与备份保留。
