# 三位授权主播数据整理

数据规模由实际有效语音和可靠对应文本决定，不设总时长、片段数量或等量要求。先完成 Zero-shot 比较，再判断是否需要 Fine-tune。

## 存放与记录

`runtime/voice-datasets/speaker-a/`、`speaker-b/`、`speaker-c/` 各自保存 `raw/` 原始素材、`audio/` 自然切片、`references/` 参考音频。整个数据目录忽略 Git；原始文件保留且不覆盖。身份对应尚未确认时不导入候选音频。

本次编号：A = 温州女生，B = 温州鞋厂，C = 红薯男生。`raw/` 使用指向下载目录完整原始 MP3 的软链接，未复制或覆盖原件；移动下载目录中的文件会使链接失效。旧的三个 `voices/*/reference-candidate.wav` 未经来源比对，不视为已确认 Reference。

每位主播建立 `sources.jsonl`，记录 source_file、原始路径、授权范围说明、duration、codec、sample_rate、channels 和对应文本路径。原始音频总时长按唯一源文件统计，派生切片不重复计入。

对齐和审核记录保存为 `alignment.jsonl`：source_file、speaker_id、start_time、end_time、text、text_source、text_verified、quality_status（pending/accepted/rejected）、rejection_reasons、reviewer。已有正确文本作为内容基准，ASR 仅辅助定位；商品名、数字、价格、重量、规格、优惠条件、人名与口语逐项核对。不修改文本原意。

质量审核覆盖：目标主播身份、其他人说话、背景音乐、掌声音效、混响、爆音、网络卡顿、静音、可懂度。未经确认的记录保持 pending。剔除表示不导出到有效集，原始文件和审核记录仍保留。

仅导出授权范围覆盖、质量 accepted、文本已校对且自然边界确认的片段到 `manifest.jsonl`。每条至少包含：utterance_id、audio_path、text、speaker_id、start_time、end_time、duration、source_file。时间以原始素材秒数记录，duration = end_time - start_time。通常 5～30 秒仅是参考，完整的 4 秒或 32 秒表达也可保留。

## 本地执行与校对

职责分为 `voice_datasets/transcription.py`（全量候选转写、断点记录）、`voice_datasets/review.py`（审核校验、统计、有效集导出），命令入口位于 `scripts/`。真实音频、转写、审核和日志均位于运行目录。

三份录音没有对应的已校对文本，先执行全量候选转写：

```bash
.venv-asr/bin/python -u scripts/voice-datasets-prepare.py \
  --model runtime/models/asr/large-v3-turbo
```

可用 `--speaker a` 单独续跑一位；默认顺序处理全部三位。使用数据目录进程锁防止重复任务。源文件、模型权重及配置的 SHA-256 和 ASR 版本、参数共同校验断点。完成的窗口直接复用；失败记录保留，另外两位仍继续执行。改变源文件或模型时拒绝混用已有断点。

运行 `python3 scripts/voice-datasets-status.py` 刷新 `runtime/voice-datasets/status.md` 和 `report.json`。每个 `asr/status.json` 在一个窗口完成后更新；根报告是明确注明生成时间的快照。未测试时不替用户选择 Primary，也不凭空给出 Fine-tune YES/NO。

ASR 以 600 秒核心范围、两侧 5 秒上下文分批运行，不是固定时长训练切片。原始窗口输出和词时间戳完整保存到 `asr/window-*.json`；`asr/candidates.jsonl` 将时间转换为原始录音坐标，按中点归属去除重复上下文，交界、重叠和可疑时间戳标记待查。窗口交界可能存在遗漏或重复，校对须对照原始窗口上下文，不能将 ASR 覆盖时长等同于可靠转写覆盖时长。派生的 16 kHz 工作 WAV 保留，不自动清理。

候选记录不是完整自然句子。人工听原音，修正文字、调整或合并边界，另存 `alignment.jsonl`。每个 accepted 记录必须明确 `text_verified: true`、`speaker_verified: true`、`natural_boundary_verified: true`，并填写 `reviewer`、唯一 `utterance_id`。拒绝的范围填写 `rejection_reasons`；最终质量决定不得彼此重叠。未校对保持 pending。

```bash
python3 scripts/voice-datasets-export.py \
  runtime/voice-datasets/speaker-a \
  runtime/voice-datasets/speaker-a/alignment.jsonl \
  runtime/voice-datasets/speaker-a/exports/review-001
```

导出前验证全部记录；仅导出通过审核的片段，生成 24 kHz 单声道 PCM WAV、manifest、对齐结果、统计与完成状态。输出目录必须是新目录，失败产物明确标记 failed 并保留，原件始终保留。对比原始格式时以 `sources.jsonl` 为准。

`technical-scan.json` 记录全文件解码、峰值/RMS、低于 -40 dB 持续至少 1 秒的静音区间。它不能证明没有其他人声、音乐、混响、听感爆音或 ASR 错词，也不会据此自动剔除数据。

每位主播的 `statistics.json` 记录 raw_audio_duration、transcript_covered_duration、usable_audio_duration、segments、average_duration、min_duration、max_duration、rejected_duration、pending_duration。文本覆盖和有效时长按各源文件时间区间取并集，避免重叠重复计时。没有素材时标记 unknown，不写成真实零时长；尚待审核不等于已剔除。

## Zero-shot 比较

从全部有效素材选择 Normal、Energetic、Calm 的高质量 Reference，数量按实际质量确定；缺失的类别如实记录。每个 Reference 关联 manifest 中的片段及准确文本。

三位主播使用 `config/voice-benchmark-texts.json` 的相同测试文本，锁定相同模型、推理参数及长文本分段策略。每次记录 speaker_id、reference_id、test_id、模型版本、参数、输出音频路径、合成耗时、输出时长和 RTF；首包延迟只有后端真实提供时才填写。测试产物保存在运行数据目录，不自动注册或替换在线音色。

本次测试文本由用户指定的 `9月4日 (1)_原文.txt` 提供。原文件按字节原样备份至 `runtime/voice-datasets/benchmark/`，配置记录 SHA-256 和原文字符偏移。短/正常/长话术分别原样摘取 48、155、849 字符，全文 60312 字符也保留供扩展长时测试。原文含“国王”“33块81件”等疑似转写问题，未擅自修改；发音评测应与实际传入文本对照，不能把文案已有错误归因于 TTS。此文件只用于合成测试，不是三份录音的训练转写。

首批三段样本经用户确认文本和语音后，记录为 accepted，并各自导出至 `speaker-*/exports/review-001/`。这只确认相应的短时间范围，不代表整份素材已校对或某一段已成为 Best Reference。用户确认的 Reference 文字保持不变。

首轮同文合成命令：

```bash
python3 scripts/voice-benchmark.py runtime/voice-datasets/benchmark/run-001
```

`voice_datasets/benchmark.py` 核对原文快照及摘录、审核记录，生成各自 prompt_speech，并以相同 backend=auto、speed=1、seed=42、threads=4、max-llm-len=4096 和默认文本分段执行三位的短/正常/长话术；实际后端以引擎日志为准。其余采样参数来自同一模型元数据。首轮入口针对当前每位一个已确认样本；后续应继续从全量素材筛选更多 Reference，不能据首轮数量停止整理。

新输出目录保留运行配置、模型/Reference 哈希、每次日志、音频及 `results.json`、`listen.md`。失败不会注册音色或替换线上配置。这里的 `cli_wall_seconds` 和 `cold_wall_rtf` 包含每次模型启动，若 ASR 同时运行也会竞争资源，不能当作常驻 Runtime 的纯推理延迟或正式性能结论。生成后仍须评价发音、真人感、直播感、漂移和长文本是否完整。

本机验证时，显式 `backend=metal` 初始化失败，而 `auto` 实际选择 `MTL0 (Apple M4 Pro)`。默认配置在部分较长输入触发 GGML 张量布局断言；仅改 f16 缓存仍可复现。`f16` 缓存并关闭 `--llm-flash-attn`、`--flow-flash-attn` 后，原先失败的 B 正常话术成功生成，因此后续对比统一采用此兼容配置。失败运行完整保留，不能把引擎错误记成主播质量差，也不因此推荐 Fine-tune。

CosyVoice 输出 `pcm_f32le` 浮点 WAV，使用 ffprobe 检查，不能用只支持 PCM 整数 WAV 的 Python `wave` 直接判定失败。`run-002` 的浮点校验误报已纠正并保留原始结果备份；引擎实际崩溃的条目仍为 failed。后续统一参数运行使用新的目录。

人工试听音色、真人感、直播感、普通介绍、高能促单、长话术稳定、数字发音、断句、音色漂移；记录评价依据和具体音频时间位置。逐位选 Best Reference，再确定 Primary / Secondary。未试听不能宣布 PASS。

Zero-shot 满足音色稳定、自然、直播感、长话术稳定、Reference 切换稳定、关键字发音和可接受延迟后再进入 Runtime。仅明确存在问题且可由训练改善时推荐 Fine-tune；先选 Primary 并使用其全部有效数据，三个 Speaker 始终独立。
