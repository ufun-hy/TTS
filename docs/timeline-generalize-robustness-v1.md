# Timeline Generalize — Direct Model V1

Generalize 默认路径已经简化为直接模型生成：

```text
timestamped segment
→ qwen3:8b 直接读取原话
→ candidates[]
→ Runtime 随机选择
→ TTS
```

程序不再分析或审核文本质量，不再生成/依赖：

- `segment_type`
- `intent`
- `facts`
- `must_keep`
- `hard_keep`
- `semantic_keep`
- `semantic_points`
- semantic coverage
- surface similarity
- duration preferred/acceptable/hard range

程序只负责：

1. 保持 segment 顺序、时间戳和 `pause_after`。
2. 要求模型返回合法 JSON：`{"candidates":[...]}`。
3. 确认 `candidates` 是非空字符串数组。
4. JSON/空结果失败时重试，并保持单 Segment 失败隔离。
5. Runtime 从模型生成的 candidates 中直接选择，不按时长给文本排序。

模型 Prompt 负责要求保留原意、价格、数字、规格、优惠和产品事实，并避免编造。

## Run

```bash
python3 scripts/timeline-generalize.py input.json runtime/timeline.json --model qwen3:8b --force
```

输出 Segment 只保留 Runtime 需要的字段和模型生成的 `candidates`。
