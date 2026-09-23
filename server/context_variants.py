"""Context groups and jointly generated variants, independent of editing and TTS."""
from __future__ import annotations

import hashlib
import copy
import json
import random
import re
from typing import Any

from server.speech_units import _linked

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
END = re.compile(r'[。！？!?；;][”’」』）)]*$')
MAX_UNITS = 5000
MAX_UNIT_CHARS = 4000
MAX_TEXT_CHARS = 1_000_000


def normalize_units(raw: Any) -> list[dict]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_UNITS:
        raise ValueError(f"paragraphs must contain 1 to {MAX_UNITS} units")
    units, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ID.fullmatch(item["id"]):
            raise ValueError("each unit needs a valid id")
        if item["id"] in seen:
            raise ValueError(f"duplicate unit: {item['id']}")
        text = item.get("original_text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_UNIT_CHARS:
            raise ValueError(f"invalid original_text: {item['id']}")
        seen.add(item["id"])
        units.append({**item, "original_text": text.strip()})
    if sum(len(p["original_text"]) for p in units) > MAX_TEXT_CHARS:
        raise ValueError("context source text is too long")
    return units


def source_fingerprint(units: list[dict]) -> str:
    value = [(p["id"], p["original_text"]) for p in units]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def build_context_groups(raw: Any) -> list[dict]:
    units = normalize_units(raw)
    groups, current = [], []
    length = 0
    for unit in units:
        text = unit["original_text"]
        if current:
            previous = current[-1]
            boundary = bool(END.search(previous["original_text"])) and not _linked(previous["original_text"], text)
            paragraph_change = (previous.get("source_paragraph_index") is not None
                                and previous.get("source_paragraph_index") != unit.get("source_paragraph_index"))
            if boundary and (length >= 240 or (length >= 120 and (len(current) >= 6 or paragraph_change))):
                groups.append(current)
                current, length = [], 0
            elif length + len(text) > 360 or len(current) >= 30:
                # A context boundary does not split a unit; neighbors remain visible to the model.
                groups.append(current)
                current, length = [], 0
        current.append(unit)
        length += len(text)
    if current:
        groups.append(current)
    return [{"id": f"g{i:04d}", "revision": 0, "paragraph_ids": [p["id"] for p in group],
             "source_fingerprint": source_fingerprint(group), "variants": []}
            for i, group in enumerate(groups, 1)]


def build_variant_prompt(units: list[dict], count: int, instruction: str = "",
                         before: str = "", after: str = "") -> str:
    normalize_units(units)
    if type(count) is not int or not 1 <= count <= 5:
        raise ValueError("candidate_count must be between 1 and 5")
    if not all(isinstance(s, str) for s in (instruction, before, after)):
        raise ValueError("instruction and neighbor context must be strings")
    return f"""你是直播话术泛化助手。把下面一整组连续话术作为完整上下文，一次写出 {count} 套连贯版本。
每套版本内部必须连续承接，统一主播口吻、节奏和语气；不是给每句独立写候选再拼接。
保留商品、价格、数量、规格、物流、售后和条件等原有事实，不能增加原稿没有的承诺或营销事实。
不总结、不重复信息、不增加无意义连接词。保留原有信息顺序和单元 ID，每个单元非空。
单元只是编辑定位点：逗号拆开的句子应连续，不能为了 ID 边界让每句重新起头。
每套输出必须包含所有目标 ID 一次，按原顺序；不要输出邻接上下文或从邻接上下文搬入事实。
只读前文：{before}
只读后文：{after}
额外要求：{instruction}
仅返回 JSON：{{"variants":[{{"segments":[{{"id":"目标ID","text":"该版本在该ID的文本"}}]}}]}}。
variants 数量必须恰好为 {count}；每套必须从首单元写到末单元。不要说明或 Markdown。
目标单元：{json.dumps([{'id': p['id'], 'text': p['original_text']} for p in units], ensure_ascii=False)}"""


def validate_variant_result(raw: Any, units: list[dict], count: int, group_id: str, revision: int) -> dict:
    expected = [p["id"] for p in units]
    variants = raw.get("variants") if isinstance(raw, dict) else None
    if not isinstance(variants, list) or len(variants) != count:
        raise ValueError(f"model must return exactly {count} complete variants")
    candidates = {identifier: [] for identifier in expected}
    for variant in variants:
        segments = variant.get("segments") if isinstance(variant, dict) else None
        if not isinstance(segments, list) or len(segments) != len(expected):
            raise ValueError("variant must cover every unit exactly once")
        if any(not isinstance(s, dict) for s in segments) or [s.get("id") for s in segments] != expected:
            raise ValueError("variant unit ids must exactly match source order (no missing, duplicate or extra ids)")
        for segment in segments:
            text = segment.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_UNIT_CHARS:
                raise ValueError(f"invalid variant text: {segment['id']}")
            candidates[segment["id"]].append(text.strip())
    descriptors = [{"id": f"{group_id}-r{revision}-v{i + 1}", "candidate_index": i} for i in range(count)]
    return {"paragraphs": [{"id": identifier, "candidates": candidates[identifier]} for identifier in expected],
            "group": {"id": group_id, "revision": revision, "paragraph_ids": expected,
                      "source_fingerprint": source_fingerprint(units), "variants": descriptors,
                      "selected_variant_id": descriptors[0]["id"]}}


def validate_context_project(paragraphs: Any, groups: Any, *, complete: bool = False) -> list[dict]:
    units = normalize_units(paragraphs)
    if not isinstance(groups, list) or not groups or len(groups) > len(units):
        raise ValueError("context_groups must cover the project")
    by_id = {p["id"]: p for p in units}
    covered, group_ids, variant_ids = [], set(), set()
    max_round = 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("id"), str) or not ID.fullmatch(group["id"]):
            raise ValueError("invalid context group id")
        if group["id"] in group_ids:
            raise ValueError("duplicate context group id")
        group_ids.add(group["id"])
        ids = group.get("paragraph_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in by_id for i in ids):
            raise ValueError("invalid context group members")
        covered.extend(ids)
        revision = group.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid group revision")
        members = [by_id[i] for i in ids]
        variants = group.get("variants")
        if not isinstance(variants, list) or len(variants) > 5 or (complete and not variants):
            raise ValueError(f"group {group['id']} needs complete variants")
        if group.get("source_fingerprint") != source_fingerprint(members):
            if complete:
                raise ValueError(f"group {group['id']} source changed; regenerate the group")
        slots = []
        for variant in variants:
            if not isinstance(variant, dict) or not isinstance(variant.get("id"), str) or not ID.fullmatch(variant["id"]):
                raise ValueError("invalid variant id")
            if variant["id"] in variant_ids:
                raise ValueError("duplicate variant id")
            variant_ids.add(variant["id"])
            slot = variant.get("candidate_index")
            if type(slot) is not int:
                raise ValueError("invalid variant candidate index")
            slots.append(slot)
        if slots != list(range(len(variants))):
            raise ValueError("variant slots must be complete and ordered")
        if variants:
            if revision < 1 or group.get("selected_variant_id") not in {v["id"] for v in variants}:
                raise ValueError("invalid selected variant or revision")
            for p in members:
                values = p.get("candidates")
                if not isinstance(values, list) or len(values) != len(variants):
                    raise ValueError(f"unit {p['id']} candidate slots do not match its group")
                if any(not isinstance(t, str) or not t.strip() or len(t) > MAX_UNIT_CHARS for t in values):
                    raise ValueError(f"unit {p['id']} has invalid candidate text")
                max_round += max(map(len, values))
    if covered != [p["id"] for p in units]:
        raise ValueError("groups must cover every unit once in contiguous source order")
    if max_round > MAX_TEXT_CHARS:
        raise ValueError("variant round is too long")
    return units


def choose_variant_round(paragraphs: list[dict], groups: list[dict], previous: list[int] | None = None,
                         rng: random.Random | None = None) -> tuple[list[dict], list[int]]:
    """Select at group granularity; callers validate the immutable session snapshot once."""
    rng = rng or random.Random()
    indexes = [rng.randrange(len(g["variants"])) for g in groups]
    if indexes == previous:
        choices = [i for i, g in enumerate(groups) if len(g["variants"]) > 1]
        if choices:
            i = rng.choice(choices)
            indexes[i] = (indexes[i] + rng.randrange(1, len(groups[i]["variants"]))) % len(groups[i]["variants"])
    by_id = {p["id"]: p for p in paragraphs}
    segments = []
    for group, index in zip(groups, indexes):
        variant = group["variants"][index]
        for identifier in group["paragraph_ids"]:
            segments.append({"id": identifier, "text": by_id[identifier]["candidates"][variant["candidate_index"]],
                             "group_id": group["id"], "variant_id": variant["id"]})
    return segments, indexes


def prepare_context_live(raw: Any) -> dict:
    """Freeze one session and reject versions that lose whole members to filtering."""
    from server.semantic_tts_blocks import safe_continuous_text

    if not isinstance(raw, dict):
        raise ValueError("context session must contain paragraphs and context_groups")
    units = validate_context_project(raw.get("paragraphs"), raw.get("context_groups"), complete=True)
    groups = raw["context_groups"]
    by_id = {p["id"]: p for p in units}
    for group in groups:
        for variant in group["variants"]:
            slot = variant["candidate_index"]
            segments = [{"id": i, "text": by_id[i]["candidates"][slot]} for i in group["paragraph_ids"]]
            safe, spans, _ = safe_continuous_text(segments)
            surviving = {s["paragraph_id"] for s in spans}
            if not safe.strip() or surviving != set(group["paragraph_ids"]):
                raise ValueError(f"group {group['id']} variant {variant['id']} has fully blocked units; edit before Live")
    return copy.deepcopy({"paragraphs": units, "context_groups": groups})
