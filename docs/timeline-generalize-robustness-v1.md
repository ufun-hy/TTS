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

Ollama 非法 JSON 最多自动重试 2 次。Generalize 默认使用 `semantic-coverage-v1`：Analyzer 提取 semantic skeleton 和 semantic_points，Rewrite 不再携带完整原文；时长分为 preferred / acceptable / hard range，acceptable 候选允许带 warning，不再用固定 ±15% 作为唯一标准。Rewrite 统一返回完整 `candidates`，程序负责 hard_keep、semantic coverage、相似度和时长审核；每段失败最多做一次针对性 Retry，Analyzer 结果不会被重复生成。

旧的 slots / combination / rescue 代码保留在 legacy mode，但 Simple Candidate V1 默认不进入这些路径。

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

报告还包含 `preferred_duration_pass`、`acceptable_duration_pass`、`simple_candidate_pass`、`candidate_count_total`、`accepted_candidate_count_total`、`analysis_calls`、`rewrite_calls`、`retry_calls` 和 `global_duration_drift`。Strategy 版本和 Segment fingerprint 会写入输出，避免策略变化后错误复用旧的 `--resume` 结果。
