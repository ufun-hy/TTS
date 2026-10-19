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

完整包的 FFmpeg 固定为上游月末构建 `autobuild-2026-09-30-13-08`（8.1.3，LGPL shared），
下载后校验 SHA256。BtbN 只保留最近 14 个日构建，月末构建保留两年；因此不能固定普通
日构建或改成浮动 `latest` 来绕过 404。到期前需更新月末版本与校验值，并重新验证原生 DLL。
安装器与 runtime 清单版本统一读取 `config/app-version.json`，CI 检查应用和 runtime 版本一致。

## 泛化大项目保存上限

项目保存独立支持最多 20000 个话术单元，不再共用单次泛化请求的 5000 条上限。
Windows 页面仍按每批 8 条串行泛化，每批完成后保存整个项目。超过 5000 条的项目
不会再因为该数量检查在首批后停止；超过 20000 条需拆分项目。HTTP 请求体仍受
16 MiB 限制，特别长的原稿或候选可能先达到此限制。保存失败时页面保留已返回结果，
以保存状态为准，不将未落盘结果宣称为已保存。

升级后应使用超过 5000 条的项目验证保存、连续完成两批及重新加载进度。

## 在线快速更新（1.1.0 起）

启动器保留一个小窗口，显示版本、源码提交和“检查更新”入口；也可运行
`AI-Live-Studio.exe --updates`，不启动模型即可检查更新。启动时后台检查只提示新版本，
网络故障不会影响启动，不会自动安装。尚未配置更新源时，手动检查会显示具体提示。

配置位于随应用分发的 `config/update-source.json`：`manifest_url` 是固定 HTTPS 清单地址，
`public_key` 是 Ed25519 原始公钥的 Base64。私钥只放发布环境 secret，不进入源码、模型包
或客户端。先在首个安装版本中预置公钥和地址，再发布签名清单；旧安装首次升级仍需离线补丁。

`config/app-version.json` 定义展示版本和严格递增的 `sequence`；每次发布都要递增。
应用清单同时保留 commit、runtime_id 和 release_sequence。客户端不安装同提交或较旧序号。
匹配 runtime_id 优先下载小补丁；不匹配时选择完整安装 ZIP（EXE 和所有 BIN 分卷），
未提供完整包则明确阻止升级。旧版 runtime_id 不同不能靠改清单假装兼容，必须发布对应完整包。

下载位于用户数据目录 `updates/downloads/`，按签名清单中的 SHA256 命名。
流式下载支持 HTTP Range 续传；服务端不支持续传时重新下载。下载后和安装前均校验，
ZIP 解压拒绝越界路径和超限内容。临时文件、备份均保留，不自动清理。

安装前保存项目、结束任务；客户端检查直播、GPU 和转写任务状态，无法确认时阻止安装。
可从开始菜单 Stop Background Services 停止服务，然后单独打开 Check for Updates。
用户确认后外部补丁工具等待启动器退出，再提权更新；沿用既有事务备份、健康检查和回滚。
补丁成功会重新启动后台服务，工具显示 PASS，随后重新打开启动器。
完整安装使用安装器原有流程，**不承诺与小补丁相同的自动回滚**，升级前保留旧安装备份。

### 发布流程

1. 选定 HTTPS 托管。推荐国内对象存储加下载域名；也可独立 GitHub Releases 仓库存放公开安装包。
   客户端不携带私有 GitHub token。配置真实 URL、公钥，递增版本，提交源码。
2. 为该精确提交构建并通过测试。运行环境改变时必须同时构建完整安装包。
   两种构建均安装 cryptography 以打入启动器，已有 Python Runtime 不因此增加依赖。
3. 在 GitHub `windows-release` environment 设置 `WINDOWS_UPDATE_SIGNING_KEY`（Ed25519 PEM），
   建议为该 environment 设置发布审批。手动运行 Prepare signed Windows release，指定成功构建 run ID、
   不可变版本下载 URL 和更新说明。工具核对构建状态、源码 SHA、应用文件哈希和签名公钥。
4. 从签名工作流获取 `latest.json`，完整包如有则使用工作流输出的 ZIP 原样上传；补丁使用
   对应构建 artifact 内的 `AI-Live-Studio-Windows-Patch.zip`。先上传包并确认可下载，最后原子替换
   固定地址的 `latest.json`。对象存储配置尚未确定，所以本工作流只生成待发布产物，不自动对外上传。
5. 安装验收后才公开清单；验证下载中断重试、损坏拒绝、不兼容包、失败恢复、用户数据保留，
   以及 5001 条项目保存与跨批泛化。Actions artifact 仍是暂存区，不作为长期更新地址。

本地签名入口 `build/windows/sign_release.py --help`。私钥放仓库外，只由发布者提供；
工具不会自动生成生产密钥。模型下载和模型增量更新不属于此版。
