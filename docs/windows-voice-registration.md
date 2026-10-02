# Windows：从录音添加音色

在 Windows 单机版 Text Studio 顶部「试听 / 智播音色」旁点击 **添加音色**：

1. 填写中文或其他显示名称。
2. 选择 WAV、MP3、M4A 或 MP4 录音，最多 50 MiB、3～30 秒，建议 8～20 秒。录音应为单人完整表达，避免音乐、插话和长静音。长录音需先裁剪；程序不会静默截取。
3. 播放参考录音，填写实际说出的原文，保留重复词，勾选文字已核对。
4. 点击 **创建音色**，保持页面打开。成功后自动选中新音色。
5. 点击 **试听新音色**，用新句子生成试听，再用于直播。

当前版本的原文由用户填写和核对，不自动转写或降噪。试听生成成功与用户确认声音效果是两项不同的验收。

## 必需文件

程序使用安装包自带的 `runtime/bin/cosyvoice/cosyvoice-cli.exe` 和 FFmpeg。
外部模型目录（默认 `D:\AI-Live-Studio-Models`，也支持启动器选择的其他目录）需要：

```text
tts/
  voices.json
  CosyVoice3-2512_Q8_0.gguf
  speech_tokenizer_v3.int8.onnx
  campplus.int8.onnx
```

旧模型包如果只有合成模型和已注册音色，还需补齐这两个 ONNX 文件；可从现有 Mac 项目的 `runtime/models/` 复制同名文件到 Windows 模型目录的 `tts/`。程序不自动下载模型。弹窗检查缺失文件并显示准确路径，不影响旧音色继续使用。

构建脚本检查并验证 CLI 入口。本次修改构建配方，因此按既有兼容性规则需要新的完整安装包，不能将其当作兼容旧运行时的小补丁。未执行 Windows 构建及真机验收时，不视为已经交付可安装的 Windows 版本。

## 文件和运行行为

- 业务实现：`voice_datasets/registration.py`；HTTP 入口：`server/voice_registration.py`；页面扩展：`web/voice-registration.js`。
- 上传原件、24 kHz 单声道 PCM16 WAV、核对后的文本、处理日志和修改前的配置备份，保存在 `%LOCALAPPDATA%\AI-Live-Studio\voice-recordings\<录音编号>\`，跟随既有 `AI_LIVE_STUDIO_DATA` 配置。
- 最终音色写入 `<模型目录>/tts/voices/voice_<录音编号>.gguf`；`voices.json` 新增 `prompt_speech` 和 `label`，保留旧条目，采用同目录原子替换。Gateway 热加载名称和配置，无需重启。
- ID 自动生成；同一上传的注册重试复用 ID，已有文件不会被静默覆盖。失败产物保留。配置在生成期间被其他程序修改时拒绝覆盖，允许重试。
- 与转写、泛化、试听和直播共用现有运行时锁。忙碌时拒绝注册并提示稍后重试；不停止活动任务。闲置的 TTS 引擎确认退出后才运行前端。
- HTTP API 仅 Windows 单机模式启用，仅允许回环地址与本机同源请求；上传限制、扩展名、实际时长和文字均由后端验证。前端模型提取最长等待 180 秒；超时终止并等待子进程退出，再释放锁。
- 只保存文件和生成模型，不会删除原件、旧音色或日志，不上传录音到第三方。

验证命令：`python -m unittest tests.test_voice_registration tests.test_voice_prompt tests.test_text_studio tests.test_live_session tests.test_windows_packaging`。

## 本次开发验证

- 在 macOS 上，100 项相关单元／集成回归通过（包含新增 5 项注册测试）。
- 隔离浏览器环境中，通过真实 FFmpeg 和 CosyVoice v0.1.3 前端，将现有 11 秒参考录音转换并生成 188,672 字节 GGUF；验证配置新增与中文音色自动选中。
- 浏览器试听交互使用固定 WAV 测试响应；不将此作为新音色真实合成或听感验收。
- 尚未运行 Windows 安装包构建、Windows 原生前端和新音色合成验收。

## 2026-10-02 目标机部署

已通过专用 SSH 密钥，以 `EDY` 连接 `192.168.137.7 / CHINAMI-LRAUI91`，将 8 个功能相关应用文件更新到 `C:\Program Files\AI Live Studio`，补齐两个 ONNX 模型到 `D:\AI-Live-Studio-Models\tts`。目标原有 CLI 已实际通过 `--help` 检查，所以本次为已核对现有运行库的定点源码维护；未替换原生程序、Python 依赖或安装器，保留已有 `runtime_id`，并在应用 manifest 中记录每个更新文件的 SHA256 和本次维护记录。这不等同于发布通用安装包或绕过通用补丁的兼容性判断。

首次 Windows 回归发现原子替换配置可能与旧文件共享时间戳，导致列表未刷新；首次更新已自动回滚。修复为同时检查修改时间、大小和文件标识，并加入“相同时间戳、相同大小的原子替换”回归测试。第二次部署在 Windows 内置 Python 上 6 项功能测试全部通过；修复后的本地相关回归 58 项通过。

成功部署备份及事务记录：

```text
C:\Users\EDY\AppData\Local\AI-Live-Studio\updates\voice-registration-20261002-r2\backup
C:\Users\EDY\AppData\Local\AI-Live-Studio\updates\voice-registration-20261002-r2\transaction.json
```

Windows 桌面会话 Session 5 实测：参考录音 11.64 秒，上传转换与原生前端生成音色耗时 4.234 秒；设备身份确认 `NVIDIA GeForce RTX 3060` 后，使用新音色实际合成 3.92 秒 WAV（188,238 字节）。输出在 `D:\AI-Live-Studio-Models\validation\voice-registration-20261002\preview.wav`，详细证据在更新目录的 `native-validation.json` 和 `native-synthesis.log`。测试音色使用隔离配置，正式音色列表仍为 `default`、`speaker_c`；未更改用户正式音色配置。实际音色相似度和扬声器听感仍由用户试听确认。

桌面程序已启动，Ollama、TTS、Audio Cache、Studio、ASR 五项健康检查通过，ownership issues 为空；页面包含添加音色扩展，`/api/voice-registration` 返回 `ready: true`、`missing: []`，运行时回到 `IDLE`。

未删除任何录音、日志、模型或备份。两次部署产物均保留；仅移除了本次创建且已结束的两个临时计划任务。
