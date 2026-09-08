# Timeline Generalize Robustness V1

`timeline-generalize.py` 现在按 Segment 隔离处理：单段 JSON、Schema、时长或相似度失败不会中断后续 Segment。

## 运行

```bash
python3 scripts/timeline-generalize.py \
  runtime/resegmented.json \
  runtime/timeline.json \
  --resume \
  --max-retries 2
```

每个成功 Segment 会立即写入输出文件。再次使用 `--resume` 时，已有 `generalize_status=completed` 的 Segment 直接复用；使用 `--force` 才会重新生成。

Ollama 非法 JSON 最多自动重试 2 次，并将温度从 `0.8` 降到 `0.3`、`0.1`。候选时长失败会携带真实失败数据重试，Analyzer 结果不会被重复生成。

失败 Segment 保留在原顺序中，并标记：

```json
{
  "generalize_status": "failed",
  "fallback": "original",
  "fallback_text": "原始话术",
  "failure_reason": "duration_too_short"
}
```

报告默认写入 `runtime/reports/generalize-<timestamp>.json`，包含成功数、失败清单、重试次数、失败原因和平均处理耗时。
