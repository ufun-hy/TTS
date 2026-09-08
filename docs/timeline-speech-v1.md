# Timeline Speech Template Engine V1

V1 把原直播的带时间戳文本拆成 Segment，先提取意图和必须保留的信息，再生成多条口语化候选。它保持 Segment 顺序和时间轴，不把整场内容一次性重写。

## 输入

```json
{
  "segments": [
    {"id": "seg_001", "start": 0, "end": 8.5, "text": "原直播话术"}
  ]
}
```

输入可以预先带 `analysis`、`variants` 或 `slots`，这些字段会被复用，方便中断后继续执行和人工修改。

## 运行

```bash
python3 scripts/timeline-generalize.py input.json runtime/timeline.json --model qwen3:8b
```

本机 Ollama 当前使用 `qwen3:8b`。生成结果包含：`segment_type`、`intent`、`facts`、`must_keep`、候选话术，以及每条候选的时长估计、表层相似度和审核结果。

## 约束

- Segment 顺序不变，重复节点保留。
- 事实强绑定内容默认使用 `atomic`。
- 欢迎、互动、过渡、促单等内容可以使用 `composable`。
- 候选目标时长默认在原段时长 ±15% 内。
- `must_keep` 缺失、过长/过短或与原句表层过于相似的候选会被拒绝。
- 本阶段只输出可供后续 TTS 使用的文本模板，不自动合成、播放或推流。

运行时接入见 [timeline-runtime-v1.md](timeline-runtime-v1.md)：按 Segment 顺序选择候选，维护 2～3 段 Lookahead 音频缓存，并把生成与播放解耦。
