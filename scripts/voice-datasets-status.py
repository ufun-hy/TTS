#!/usr/bin/env python3
"""Write a current inventory/progress report without treating ASR as verified data."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_datasets.review import read_jsonl, statistics
from voice_datasets.transcription import write_json


def duration(value):
    if value is None:
        return "未确认"
    seconds = round(value)
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m {seconds % 60:02d}s"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", type=Path, default=ROOT / "runtime/voice-datasets")
    args = parser.parse_args()
    report = {"Result": "PARTIAL", "updated_at": datetime.now().astimezone().isoformat(), "speakers": {}}
    runs = sorted((args.datasets / "benchmark").glob("run-*/results.json"))
    benchmark_path = runs[-1] if runs else None
    benchmark_results = json.loads(benchmark_path.read_text()) if benchmark_path else []
    feedback_path = benchmark_path.parent / "user-feedback.json" if benchmark_path else None
    feedback = json.loads(feedback_path.read_text()) if feedback_path and feedback_path.exists() else None
    report["user_listening_feedback"] = feedback
    reviews = ([feedback] + feedback.get("additional_speaker_reviews", [])) if feedback else []
    report["latest_benchmark_results"] = str(benchmark_path) if benchmark_path else None
    lines = ["# 三位授权主播语音数据报告", "", "Result: PARTIAL", "", f"统计时间：{report['updated_at']}", "",
             "原始素材保留全部时长；所有 ASR 结果仍需校对和音质审核。", "",
             "| 指标 | Speaker A | Speaker B | Speaker C |", "| --- | --- | --- | --- |"]
    for letter in "abc":
        folder = args.datasets / f"speaker-{letter}"
        sources = read_jsonl(folder / "sources.jsonl")
        alignment = folder / "alignment.jsonl"
        stats = statistics(read_jsonl(alignment), sources) if alignment.exists() else json.loads((folder / "statistics.json").read_text())
        progress_path = folder / "asr/status.json"
        progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"state": "queued", "processed_audio_duration": 0, "candidate_count": 0}
        scan_path = folder / "technical-scan.json"
        scan = json.loads(scan_path.read_text()) if scan_path.exists() else {}
        speaker_results = [row for row in benchmark_results if row.get("speaker_id") == f"speaker_{letter}"]
        generated = sum(row.get("state") == "generated_pending_review" for row in speaker_results)
        listening_review = next((review for review in reviews if review.get("speaker_id") == f"speaker_{letter}"), None)
        reference_path = folder / "references/review-001.json"
        reference = json.loads(reference_path.read_text()) if reference_path.exists() else {}
        report["speakers"][letter] = {"sources": sources, "statistics": stats, "asr": progress,
                                     "technical_scan": scan, "best_reference": None,
                                     "reviewed_first_reference": reference.get("quality_status") == "accepted",
                                     "zero_shot_result": ("用户反馈：" + "；".join(f"{key}: {value}" for key, value in listening_review["tests"].items()) if listening_review else f"{generated}/3 generated; listening review pending") if speaker_results else "NOT_TESTED",
                                     "benchmark_results": speaker_results, "livestream_quality": "NOT_REVIEWED",
                                     "long_form_stability": "NOT_TESTED"}
    speakers = list(report["speakers"].values())
    fields = [
        ("原始文件", lambda s: ", ".join(source["source_file"] for source in s["sources"])),
        ("raw audio duration", lambda s: duration(s["statistics"]["raw_audio_duration"])),
        ("transcript covered duration（可靠）", lambda s: duration(s["statistics"]["transcript_covered_duration"])),
        ("usable duration", lambda s: duration(s["statistics"]["usable_audio_duration"])),
        ("segments（已确认）", lambda s: str(s["statistics"]["segments"])),
        ("ASR 已处理原音", lambda s: duration(s["asr"]["processed_audio_duration"])),
        ("ASR 候选片段数", lambda s: str(s["asr"]["candidate_count"])),
        ("ASR 状态", lambda s: s["asr"]["state"]),
        ("阈值静音区间总时长（非剔除）", lambda s: duration(s["technical_scan"].get("silence_duration"))),
        ("best reference", lambda s: "首批已确认，Best 待比较" if s["reviewed_first_reference"] else "待校对与筛选"),
        ("zero-shot result", lambda s: s["zero_shot_result"]),
        ("livestream quality", lambda s: "未评估"),
        ("long-form stability", lambda s: "未测试"),
    ]
    for label, getter in fields:
        lines.append("| " + " | ".join([label] + [getter(s) for s in speakers]) + " |")
    total = sum(s["statistics"]["raw_audio_duration"] for s in speakers)
    report.update({"raw_total_duration": total, "Primary Speaker": None, "Secondary Speaker": None,
                   "Zero-shot ready": "NO", "Fine-tune recommended": "PENDING",
                   "Main reason": "原始录音没有配套转写；仅已确认样本范围计入有效集，其余 ASR 仍待校对。合成成功不能代替音色、发音与长话术试听评价。",
                   "Next step": "继续全量 ASR 和素材审核；完成相同参数的 A/B/C 测试后试听比较，再决定 Primary 与是否训练。"})
    lines += ["", f"原始素材合计：{duration(total)}。三份均为 MP3、44100 Hz、双声道。", "",
              "Primary Speaker: 未确定", "", "Secondary Speaker: 未确定", "",
              "Zero-shot ready: NO（尚未验证）", "", "Fine-tune recommended: PENDING（尚无测试依据作 YES/NO 决策）", "",
              f"Main reason: {report['Main reason']}", "", f"Next step: {report['Next step']}", "",
              "统一测试原文和短/正常/长话术已记录于 `config/voice-benchmark-texts.json`，保留原文及 SHA-256；不作为三个 Speaker 的对应转写。", "",
              "[首批 Reference 校对样本](reference-review.md)", "",
              f"最新合成结果：`{benchmark_path}`。" if benchmark_path else "尚无合成结果。", "",
              "全量 ASR 当前状态分别见各 Speaker 的 `asr/status.json`；本报告为生成时快照。重新运行 `python3 scripts/voice-datasets-status.py` 可刷新。", "",
              "全文件技术扫描已保存原始日志和 JSON，但其他人声、背景音乐、掌声音效、混响、网络卡顿、可懂度及听感爆音仍待审核。没有据技术指标自动剔除或删除音频。", "",
              "运行数据还保留 ASR 工作 WAV、校对样本和明确标注的合成测试夹具 `_validation/`，它们不计入主播原始素材或有效集。"]
    write_json(args.datasets / "report.json", report)
    if feedback:
        lines += ["", "## 已收到的试听反馈", "", feedback["quote"], "",
                  "C 列为当前首选候选，尚非最终 Primary 决策。此反馈只覆盖当前 Reference 的三组音频。", "",
                  "当前无试听问题支持启动 Fine-tune；继续验证：" + "、".join(feedback["unverified"]) + "。"]
        for review in feedback.get("additional_speaker_reviews", []):
            lines += ["", f"### {review['speaker_id']}", "", review["quote"], "", review["scope"]]
    (args.datasets / "status.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"PARTIAL: raw={duration(total)}; report={args.datasets / 'status.md'}")


if __name__ == "__main__":
    main()
