# Windows 单机播放生命周期验收记录

记录日期：2026-09-27，Asia/Shanghai。

## 固定起点与当前提交

验收基线见 [Windows 验收固定基线](windows-acceptance-baseline.md)：main 的 7 个提交和 Windows 分支起点 `d7a24c3d19bacb7fc4ad4d899f78cad6f4920440` 已冻结。

本轮 Windows 分支提交：

- `8ea7dd9`：将 main 的缓存播放调度优化适配到 Windows 单机分支，保留严格会话字段。
- `ce10c3a`：暂停/停止联动、Audio Cache 会话控制、严格会话重绑和生命周期测试。
- `c8eb9ce`：复制固定验收基线文档到 Windows 分支。

当前 HEAD 在本记录提交前为 `c8eb9ce`；本记录提交后以新的 HEAD 为准。远端分支没有推送。

## 已完成的实现

Windows 单机现在把 Live Session 的 `starting/running/paused/stopping/stopped` 写入 Audio Cache。GUI 和无界面播放服务读取该状态：暂停时暂停播放并停止领取下一项，继续时恢复，停止时停止当前播放并等待新会话。动态严格会话停止后会清空本地绑定，下一次收到新 Session 时从 sequence 1 重新绑定；严格模式的状态查询不会按“最新下载”删除旧会话。

播放优化已在 `8ea7dd9` 进入 Windows 分支：增量 metadata 索引、合并状态写入、PCM16 单次检查和阶段计时均保留；严格会话仍使用显式 `session_id` 与期望 `sequence`，没有用 main 播放器整文件覆盖单机入口。

## 可重复验证

执行目录：`/Users/ufun/code/2026/TTS-windows-offline-runtime-v1`。

- `python3 -m unittest discover -s tests`：248 项通过，3 项跳过（Windows WinMM/launchd 平台或显式配置项）。
- `python3 -m py_compile audio_client/*.py audio_cache/*.py server/*.py windows_playback_service.py windows_launcher.py tests/test_windows_playback_lifecycle.py`：通过。
- 新增测试覆盖：暂停期间不领取下一项、停止后保留当前段、严格会话停止后绑定新 Session、Audio Cache 控制状态往返、GUI 暂停/停止联动。
- `git diff --check`：通过。

## 真机状态

本次执行环境为 macOS，未发现可运行 Windows 11/WinMM 的目标机或远程 Windows 执行端。因此没有把 fake player、Python 回归或 macOS 模拟结果写成真机通过；以下项目仍待目标 Windows 设备完成：

`开播 → 暂停 → 继续 → 停止 → 换稿再开播` 的真实声音、MCI 暂停位置、音频设备停止响应、真实 Audio Cache/Studio 进程联动、Windows 安装包启动和 WinMM Float32/PCM16 路径。

在这项真机序列通过前，不推进首轮准备和完整候选随机播报迁移，也不宣称 Windows 单机 V1 播放验收完成。
