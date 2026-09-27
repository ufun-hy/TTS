"""Build a stable first-round plan from the generated prefix of a project."""
from __future__ import annotations

import hashlib
import json
import random

from server.context_variants import END, prepare_context_live
from server.prohibited_speech import broadcast_text, candidate_reviews, filter_segments, RULES
from server.semantic_tts_blocks import semantic_blocks
from server.live_session import prepare_candidate_pools, prepare_live_segments, _DYNAMIC_TIME_TOKEN
from server.live_session_blocks import prepare_synthesis_blocks


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def project_inputs(project: dict) -> tuple[list[dict], list | None, bool, int]:
    """Match the editor's Live payload, stopping at the first unfinished unit/group."""
    paragraphs = project.get("paragraphs", [])
    groups = project.get("context_groups")
    if groups is not None:
        ready_groups = []
        by_id = {p["id"]: p for p in paragraphs}
        for group in groups:
            count = len(group.get("variants", []))
            if not count or any(len(by_id[i].get("candidates", [])) != count for i in group["paragraph_ids"]):
                break
            ready_groups.append(group)
        ids = [i for group in ready_groups for i in group["paragraph_ids"]]
        units = [{"id": i, "original_text": by_id[i]["original_text"],
                  "candidates": list(by_id[i]["candidates"]),
                  "prohibited_reviews": by_id[i].get("prohibited_reviews", [])} for i in ids]
        return units, ready_groups, len(ready_groups) == len(groups), len(paragraphs)
    units = []
    for p in paragraphs:
        if not p.get("candidates"):
            break
        values = list(p["candidates"])
        index = p.get("selectedIndex", 0) or 0
        if p.get("editedText") and 0 <= index < len(values):
            values[index] = p["editedText"].strip()
        units.append({"id": p["id"], "candidates": values,
                      "prohibited_reviews": p.get("prohibited_reviews", [])})
    return units, None, bool(paragraphs) and len(units) == len(paragraphs), len(paragraphs)


def normalized_source(units: list[dict], groups: list | None):
    if groups is not None:
        return prepare_context_live({"paragraphs": units, "context_groups": groups})
    if not isinstance(units, list) or any(not isinstance(p, dict) or not isinstance(p.get("candidates"), list)
            or not p["candidates"] or any(not isinstance(t, str) or not t.strip() for t in p["candidates"])
            for p in units):
        raise ValueError("每个单元需要有效的 candidates")
    # Empty filtered units are intentional and must not stop the generated prefix.
    eligible = [p for p in units if any(broadcast_text(t.strip(), candidate_reviews(p, i, t))
                                      for i, t in enumerate(p["candidates"]))]
    return prepare_candidate_pools(filter_segments(eligible)) if eligible else []


def source_signature(source, voice: str, fingerprint: str) -> str:
    return digest({"source": source, "voice": voice, "voice_fingerprint": fingerprint,
                   "rules": RULES})


def build_plan(project: dict, fingerprint: str, previous: dict | None = None, rng=None) -> dict:
    rng = rng or random.Random()
    old = (previous or {}).get("choices", {})
    units, groups, complete, total = project_inputs(project)
    plan = {"project_id": project["project_id"], "voice": project.get("voice", "default"),
            "voice_fingerprint": fingerprint, "complete": complete,
            "generated_units": len(units), "total_units": total, "choices": {},
            "blocks": [], "candidate_indexes": [], "variant_selection": [], "issue": ""}
    if not units:
        plan["source_signature"] = ""
        return plan
    source = normalized_source(units, groups)
    plan["source_signature"] = source_signature(source, plan["voice"], fingerprint)
    selected = []
    if groups is None:
        for pool in source:
            values = pool["candidates"]
            prior = old.get(pool["id"], {})
            if prior.get("text") in values:
                index = values.index(prior["text"])
            elif type(prior.get("index")) is int and 0 <= prior["index"] < len(values):
                index = prior["index"]
            else:
                index = rng.randrange(len(values))
            plan["choices"][pool["id"]] = {"index": index, "text": values[index]}
            plan["candidate_indexes"].append(index)
            selected.append({"id": pool["id"], "text": values[index]})
    else:
        by_id = {p["id"]: p for p in source["paragraphs"]}
        for group in source["context_groups"]:
            variants = group["variants"]
            prior = old.get(group["id"], {})
            indexes = [i for i, v in enumerate(variants) if v["id"] == prior.get("variant_id")]
            index = indexes[0] if indexes else rng.randrange(len(variants))
            variant = variants[index]
            plan["choices"][group["id"]] = {"variant_id": variant["id"]}
            plan["candidate_indexes"].append(index)
            plan["variant_selection"].append({"group_id": group["id"], "revision": group["revision"],
                                               "variant_id": variant["id"]})
            for identifier in group["paragraph_ids"]:
                p = by_id[identifier]
                text = p["candidates"][variant["candidate_index"]]
                selected.append({"id": identifier, "text": text, "group_id": group["id"],
                                 "variant_id": variant["id"],
                                 "prohibited_reviews": candidate_reviews(p, variant["candidate_index"], text)})
    if not complete:
        # Only process complete sentences. Future units may complete a sentence
        # (including a prohibited claim), so never synthesize its partial prefix.
        ends = [i for i, s in enumerate(selected) if END.search(s["text"].strip())]
        selected = selected[:ends[-1] + 1] if ends else []
    if selected:
        blocks = semantic_blocks(selected) if groups is not None else prepare_synthesis_blocks(prepare_live_segments(selected))
        if not complete:
            blocks = blocks[:-1]  # The greedy tail may absorb the next batch.
        if any(_DYNAMIC_TIME_TOKEN.search(b["text"]) for b in blocks):
            raise ValueError("提前准备暂不支持实时日期/时间占位符，请修改后继续。")
        plan["blocks"] = [{**b, "audio_key": digest([plan["voice"], fingerprint, b["text"]])} for b in blocks]
    if complete and not plan["blocks"]:
        plan["issue"] = "没有可播报内容，请修改或确认被屏蔽的话术。"
    return plan
