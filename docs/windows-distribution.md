# Windows 测试版安装与离线小补丁

## 新电脑：先安装完整包

1. 从 `codex/windows-offline-runtime-v1` 对应的成功 GitHub Actions run 下载
   `AI-Live-Studio-Windows-Test-Setup` artifact。不要使用旧 Audio Client 包。
2. 解压整个 artifact。保留 `AI-Live-Studio-Windows-Test-Setup.exe` 与所有
   `AI-Live-Studio-Windows-Test-Setup-*.bin` 在同一目录。**不能只复制 EXE**。
   `SHA256SUMS.txt` 覆盖每个安装分卷；`app-manifest.json` 记录构建提交。
3. 安装 NVIDIA Driver。双击 Setup EXE；无需系统 Python、pip、Ollama、FFmpeg 或 CUDA Toolkit。
4. 单独复制完整模型包到任意本地目录（默认 `D:\AI-Live-Studio-Models`）。
   除 ASR/LLM/TTS 模型，还需要 `tts/voices.json` 及其中引用的真实音色 prompt GGUF。
   只复制 CosyVoice 模型文件不等于已经准备好音色。
5. 双击 **AI Live Studio**。模型目录不同可用开始菜单 **Change Model Directory**。
   首次未导入 LLM 时，使用既有 Runtime 的 `import-llm` 诊断入口；不要复制 Mac Ollama store。
6. 验收 Text Studio、Runtime health、模型识别、一次新文本生成及实际人声播放。
   GitHub runner 没有 RTX3060，CI 成功不能替代新电脑的 GPU/声卡验收。

程序默认在 `C:\Program Files\AI Live Studio`；用户数据在
`%LOCALAPPDATA%\AI-Live-Studio`（或既有 `AI_LIVE_STUDIO_DATA`）。
模型和数据均不在安装器/补丁的应用载荷中。卸载保留外部模型及用户数据。
不要把另一台电脑的 DPAPI 文件当作可迁移密钥；新电脑应重新填写需要的凭据。

## 后续功能更新：小补丁

适用于**已经安装本次新完整包**的电脑；旧测试机没有版本基线，必须先升级完整包。
运行库、依赖或构建配方发生变化时，也必须使用新完整包；补丁会拒绝不兼容安装。

1. 结束直播/录音并关闭启动器窗口，保存工作。补丁会停止当前 Runtime。
2. 下载目标提交对应的 `AI-Live-Studio-Windows-Patch` artifact，核对 ZIP SHA256。
   只使用可信仓库的 artifact；校验和不是代码签名，也不证明陌生来源可信。
3. 解压 ZIP 到程序目录**之外**，双击 `Apply-Patch.cmd`，接受管理员权限提示。
   使用平时运行 AI Live Studio 的同一个 Windows 账号；不要改用另一个管理员账号。
4. 工具校验应用版本、现有文件、补丁文件、关键 GPU DLL，然后备份、停止、替换并启动。
   只有五个 health endpoint 和所有受管进程 ownership 正常才显示 PASS。
   不下载或安装任何依赖，不改模型、音色、项目、缓存和用户配置。
5. 看到 PASS 后双击 AI Live Studio 打开 Text Studio，并试听一段新生成的语音。
   health 成功只证明服务就绪，不证明 GPU 音频正确；仍需用户听音验收。

非默认安装/数据目录可在 PowerShell 指定：

```powershell
.\Apply-Patch.ps1 -InstallDir 'E:\Apps\AI Live Studio' -DataDir 'E:\AI-Live-Studio-Data'
```

补丁包含全部受 Git 管理的应用模块、Web 静态文件及重新编译的启动器 EXE；
不包含 Python、PyTorch、CUDA、Ollama、CosyVoice、FFmpeg 或模型。
旧版本删除的源码会移到备份 `retired` 下，不保留在程序目录继续被导入。
发现手工改动、未受管同名文件、DLL 不匹配或 ownership 不明时，先拒绝更新，不强制覆盖/杀进程。

## 回滚和断电恢复

每次补丁在数据目录 `updates/<时间-编号>` 保存旧文件和 `transaction.json`，并显示备份路径。
不自动清理备份。启动/健康检查失败时自动恢复旧应用文件，Runtime 保持停止，避免继续运行坏版本。
若恢复也失败，保留事务日志及所有文件，输出具体错误；不要直接重复覆盖。
断电或进程退出留下未完成事务时，下一次补丁被阻止，先用原补丁包执行：

```powershell
.\Apply-Patch.ps1 -Backup 'C:\Users\你的用户名\AppData\Local\AI-Live-Studio\updates\时间-编号'
```

非默认路径同时传入 `-InstallDir`、`-DataDir`。回滚后双击 AI Live Studio 重启。
回滚会校验备份及当前文件；有后续手工改动时拒绝覆盖。新加文件保留在备份 `displaced` 下。
这不是在线自动更新、代码签名或跨 Runtime 降级系统；完整安装器升级不提供事务回滚。
旧安装升级前请自行备份程序目录；安装器会先要求旧 Runtime 能安全停止。

## 2026-09-29 旧测试机审计

- 测试机应用源文件与已提交 `d7a24c3` 一致（比较已考虑 CRLF）；未发现遗漏的手工源码修复。
- 工作分支 `8cc2626` 及之前七个后续提交主要为播放生命周期修复及文档，需随新包重新验收。
- 测试机 CosyVoice server / cosyvoice.dll / ONNX 与官方 v0.1.3 1616b12 no-ICU 包一致。
- GGML core / Vulkan / OpenMP 与官方 llama.cpp b10938 Vulkan 包逐文件 SHA256 一致。
  固定哈希见 `build/windows/runtime-profile.json`，构建和补丁均校验。
- 测试机残留 `ggml-cuda.dll`，旧 runtime-manifest 仍写 CUDA。新安装明确使用
  **TTS Vulkan GPU，ASR CUDA PyTorch**；新包不分发该 GGML CUDA DLL。
- Python 依赖版本来自测试机 `runtime/python-lock.txt`，固定在 `python-constraints.txt`；
  版本约束并非 wheel 哈希锁。发布包可由外层 SHA256 校验，依赖仍从固定版本上游构建。
- 不迁移测试机日志、缓存、运行 PID、DPAPI 或个人配置进入软件包；模型/自定义音色单独备份迁移。
- 先前 RTX3060 无缓存 1000 次生成通过属于测试机已验证配置，不代表本次新安装包已经真机验收。

构建入口：`.github/workflows/windows-client-build.yml`（完整包）和
`.github/workflows/windows-patch-build.yml`（小补丁）。本文件描述操作及边界，
实际构建是否成功请以对应提交的 Actions 结果为准。
