# Timestamp-first ASR Context Grouping

ASR 重组阶段只做两件事：

1. 按时间顺序把连续的 ASR 小碎片合成较大的上下文块。
2. 按原始时间戳保留主播真实停顿。

它不再判断价格、优惠、CTA、互动、商品卖点、语义类型或“自然句”。文本在哪里断开不负责表达语义；上下文块只是给 Direct Model Generalize 足够的上下文。

## 默认规则

- `max_context_duration = 15s`：纯技术性的上下文块上限。
- `pause_threshold = 0.05s`：相邻 ASR Segment 出现真实时间间隔时，新开一个块。
- 因长度上限切开的相邻块，如果时间轴连续，则 `pause_after = 0`，Runtime 连续播放。
- 因真实停顿切开的块，`pause_after = next.start - current.speech_end`，Runtime 按原停顿等待。

没有 Semantic Boundary、价格/CTA 规则、标点规则或内容 Judge。

## 使用

```bash
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json --dry-run
```

可选：

```bash
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json \
  --max-context-duration 15 \
  --pause-threshold 0.05
```

输出 Segment 只保留时间轴和来源追踪所需字段：

- `id`
- `source_segment_ids`
- `start`
- `speech_end`
- `end`
- `speech_duration`
- `pause_after`
- `timeline_duration`
- `text`

随后直接交给 Direct Model Generalize：

```bash
python3 scripts/timeline-generalize.py runtime/resegmented.json runtime/timeline.json
```

职责划分：

```text
ASR              -> 文本 + 时间戳
Context Grouping -> 简单拼接 + 保留停顿
LLM              -> 自然改写文本
Runtime          -> 按时间顺序和 pause 播放
TTS              -> 合成声音
```
