# ASR Semantic Re-segmentation V1

重切分阶段先对 timestamped ASR 做 Sentence Reconstruction，再形成 Natural Segment；整个阶段不改写、不排序、不调用 Ollama。连续的 1～3 秒 ASR 碎片会按自然表达、真实 pause、强标点、语义边界和目标时长合并，输出保留 `source_segment_ids` 和 `reconstruction_source`。

## 使用

```bash
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json --dry-run
```

也支持纯文本测试输入。文本按空行拆成粗 Segment，并估算连续时间轴：

```bash
python3 scripts/timeline-resegment.py "/path/to/live.txt" runtime/resegmented.json --dry-run
```

默认目标：单段最多 25 秒，优选 6～15 秒，短段允许约 3 秒。输出会保留 `parent_segment_id`、`source_start`、`source_end`、`source_segment_ids`，并增加 `speech_end`、`speech_duration`、`pause_after`、`timeline_duration`、`semantic_type`、`semantic_boundary`、`boundary_source` 和 `reconstruction_source`。报告额外统计输入/输出段数、合并段数和时长分布。

有 word/sentence timestamp 时，真实停顿不会被压平；Pause 保留在 Segment 的 `pause_after` 中，由 Runtime 在下一段播放前等待。没有细时间戳的旧数据默认 `pause_after=0`，不会人为添加停顿。

重切分后可直接交给现有文本泛化：

```bash
python3 scripts/timeline-generalize.py runtime/resegmented.json runtime/timeline.json
```

重切分失败会返回错误，不会静默丢弃原文或继续生成不完整结果。
