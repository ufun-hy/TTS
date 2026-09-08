# ASR Semantic Re-segmentation V1

重切分阶段只拆 Segment，不改写、不排序、不调用 Ollama。它优先使用 `sentences`/`words` 时间戳；没有细粒度时间戳时使用标点、语义句块和加权时长估算。

## 使用

```bash
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json --dry-run
```

也支持纯文本测试输入。文本按空行拆成粗 Segment，并估算连续时间轴：

```bash
python3 scripts/timeline-resegment.py "/path/to/live.txt" runtime/resegmented.json --dry-run
```

默认目标：单段最多 25 秒，优选不超过 15 秒，短段允许约 3 秒。输出会保留 `parent_segment_id`、`source_start`、`source_end`，并生成文本完整性、时间连续性和时长分布报告。

重切分后可直接交给现有文本泛化：

```bash
python3 scripts/timeline-generalize.py runtime/resegmented.json runtime/timeline.json
```

重切分失败会返回错误，不会静默丢弃原文或继续生成不完整结果。
