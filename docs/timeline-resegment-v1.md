# Timestamp-first ASR Context Grouping

ASR 重组阶段只做两件事：

1. 按时间顺序把连续的 ASR 小碎片合成较大的上下文块。
2. 按原始时间戳保留主播真实停顿。

它不判断价格、优惠、CTA、互动、商品卖点、语义类型或“自然句”。对 TTS 来说，正常边界优先来自主播真实停顿；不要因为短时间技术切分让 TTS 反复重新起音。

## 默认规则

- `pause_threshold = 0.05s`：相邻 ASR Segment 出现真实时间间隔时，新开一个块；这是正常的 TTS 边界。
- `max_context_duration = 45s`：仅作为连续讲话过长时的安全兜底，不是常规断句规则。
- 因真实停顿切开的块，`pause_after = next.start - current.speech_end`，Runtime 按原停顿等待。
- 只有连续讲话超过安全上限时才允许技术切分；这种边界不人为增加 pause。

没有 Semantic Boundary、价格/CTA 规则、标点规则或内容 Judge。

当前 5 分钟桃子样本中，45 秒安全上限不会触发：原来 15 秒默认值产生的 11 个技术切分会消失，最终主要按 16 个真实 pause 形成约 17 个连续讲话块。

## 使用

```bash
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json --dry-run
```

可选：

```bash
python3 scripts/timeline-resegment.py input.json runtime/resegmented.json \
  --max-context-duration 45 \
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
Context Grouping -> 以真实 pause 为主的连续讲话块
LLM              -> 自然改写文本
Runtime          -> 按时间顺序和 pause 播放
TTS              -> 每个连续讲话块只合成一次，避免无意义重新起音
```
