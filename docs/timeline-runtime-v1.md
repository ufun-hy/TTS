# Timeline Runtime + Lookahead TTS Buffer V1

运行阶段不再调用 Ollama。它直接读取已经生成的 `runtime/timeline.json`，在每次 Session 中重新选择候选话术，然后通过本机 TTS Gateway 的内部 `/synthesize` 接口生成 WAV。

## Dry Run

```bash
python3 scripts/timeline-run.py runtime/timeline.json --dry-run --lookahead 3
```

Dry Run 会执行 Runtime Selector、冷却、Composable 组合、Lookahead 和 Session 落盘，但不会请求 TTS 或播放音频。

## 正式运行

```bash
python3 scripts/timeline-run.py runtime/timeline.json --voice default --lookahead 3
```

每次运行都会创建独立 Session：

```text
runtime/sessions/<session_id>.json
runtime/reports/<session_id>.json
runtime/audio-cache/<timeline_id>/<session_id>/
```

Runtime 严格按 Segment 顺序播放。生成使用单 worker，当前音频播放期间生成未来 Segment；较远 Segment 不能阻塞下一个 Segment。候选实际 WAV 时长超出目标 ±15% 时，最多重新选择并合成 2 次，之后使用最接近的版本并记录偏差。

现有 `/speak`、`/voices`、`/health` 保持兼容。`/synthesize` 仅允许本机请求，并要求现有 API Key，用于 Runtime 获取 WAV，不执行播放。
