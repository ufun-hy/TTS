"""Direct model-driven timeline speech generalization."""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib, json, random, re, time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, request

GENERALIZE_STRATEGY_VERSION="direct-model-v1"

class TimelineError(RuntimeError): pass
class SegmentFailure(TimelineError):
    def __init__(self, reason, stage, message, details=None):
        super().__init__(message); self.reason=reason; self.stage=stage; self.details=details or {}
class LLMResponseError(SegmentFailure): pass

def estimate_duration(text:str, chars_per_second:float=4.5)->float:
    if chars_per_second<=0: raise ValueError("chars_per_second must be positive")
    content=re.sub(r"\s+","",text)
    if not content: return 0.0
    punctuation=len(re.findall(r"[，。！？；：、,.!?;:]",content))
    return len(content)/chars_per_second + punctuation*0.12

def _clean_text(v): return re.sub(r"\s+"," ",str(v or "")).strip()

def _json_from_response(raw:str)->Dict[str,Any]:
    value=raw.strip()
    value=re.sub(r"^```(?:json)?\s*","",value,flags=re.I)
    value=re.sub(r"\s*```$","",value)
    try: decoded=json.loads(value)
    except json.JSONDecodeError:
        decoder=json.JSONDecoder(); decoded=None
        for m in re.finditer(r"\{",value):
            try: decoded,_=decoder.raw_decode(value[m.start():]); break
            except json.JSONDecodeError: pass
        if decoded is None: raise ValueError("Ollama returned no JSON object")
    if not isinstance(decoded,dict): raise ValueError("Ollama response must be a JSON object")
    return decoded

class Ollama:
    def __init__(self,model,base_url="http://127.0.0.1:11434",timeout=180,json_retry_count=2):
        self.model=model; self.base_url=base_url.rstrip("/"); self.timeout=timeout; self.json_retry_count=max(0,json_retry_count)
        self.last_json_retry_count=0; self.last_latency_ms=0.0
    def json(self,prompt,schema_hint="",temperature=None):
        started=time.monotonic()
        temps=([temperature,max(0.1,temperature-0.2),0.1] if temperature is not None else [0.7,0.3,0.1])[:self.json_retry_count+1]
        last_error="invalid JSON"
        for attempt,temp in enumerate(temps):
            retry_prompt=prompt if attempt==0 else prompt+"\n上一轮返回无法解析。不要解释，不要 Markdown，只返回 JSON Object。格式："+schema_hint
            payload=json.dumps({"model":self.model,"prompt":retry_prompt,"stream":False,"format":"json","options":{"temperature":temp}}).encode()
            req=request.Request(f"{self.base_url}/api/generate",data=payload,headers={"Content-Type":"application/json"},method="POST")
            try:
                with request.urlopen(req,timeout=self.timeout) as response: body=json.load(response)
                if not isinstance(body,dict) or not isinstance(body.get("response"),str): raise ValueError("Ollama response has no text payload")
                decoded=_json_from_response(body["response"])
                self.last_json_retry_count=attempt; self.last_latency_ms=(time.monotonic()-started)*1000
                return decoded
            except ValueError as exc:
                last_error=str(exc); self.last_json_retry_count=attempt
            except (OSError,error.URLError) as exc:
                reason="ollama_timeout" if "timed out" in str(exc).lower() else "ollama_connection_error"
                raise LLMResponseError(reason,"llm",str(exc)) from exc
        self.last_latency_ms=(time.monotonic()-started)*1000
        raise LLMResponseError("json_parse_error","llm",last_error)

@dataclass
class Analysis:
    segment_type:str="other"; intent:str=""; facts:List[str]=field(default_factory=list); must_keep:List[str]=field(default_factory=list)
    hard_keep:List[str]=field(default_factory=list); semantic_keep:List[str]=field(default_factory=list); semantic_points:List[str]=field(default_factory=list)
    tone:str=""; mode:str="atomic"
@dataclass
class Slot:
    id:str; required:bool; variants:List[str]
@dataclass
class Segment:
    id:str; start:float; end:float; original_text:str; analysis:Optional[Analysis]=None
    variants:List[str]=field(default_factory=list); slots:List[Slot]=field(default_factory=list); candidates:List[str]=field(default_factory=list)
    metadata:Dict[str,Any]=field(default_factory=dict); speech_end:Optional[float]=None; pause_after:float=0.0; strategy:Optional[str]=None
    @property
    def target_duration(self):
        return max(0.0,(self.speech_end if self.speech_end is not None else self.end)-self.start)
    @property
    def mode(self): return "atomic"

def _rewrite_prompt(segment,count):
    return f"""你是直播话术改写助手。
请把下面原话直接改写成 {count} 个自然、完整、可直接朗读的直播口语版本。
要求：
- 保持原话的意思和事实。
- 数字、价格、数量、规格、优惠条件、产品名等关键信息不要改错。
- 不要编造原话没有的信息。
- 不要为了凑时长故意加废话，也不要故意把内容压缩成摘要。
- 每个版本都应该是一段完整的话，不要拆成标签、要点、slot 或分析字段。
- 表达可以自然变化，不要求程序化同义替换。
原话：
{segment.original_text}
只返回 JSON：
{{"candidates":["版本1","版本2","版本3"]}}
"""

def _review(text,segment,analysis=None,tolerance=0.15,check_duration=True,check_must_keep=True,duration_policy=None):
    del segment,analysis,tolerance,check_duration,check_must_keep,duration_policy
    cleaned=_clean_text(text)
    return {"accepted":bool(cleaned),"reasons":[] if cleaned else ["empty_text"]}

class TimelineEngine:
    def __init__(self,llm,variant_count=4,duration_tolerance=0.15,cooldown_window=3,seed=None,natural_duration=True,simple_candidate=True):
        del duration_tolerance,natural_duration,simple_candidate
        if variant_count<1 or cooldown_window<0: raise ValueError("invalid timeline configuration")
        self.llm=llm; self.variant_count=variant_count; self.cooldown_window=cooldown_window; self.random=random.Random(seed)
        self.rewrite_calls=0; self.retry_calls=0; self.json_retry_total=0; self.llm_latency_total_ms=0.0
    def analyze(self,segment):
        if segment.analysis is None: segment.analysis=Analysis()
        return segment.analysis
    def _ask(self,prompt,schema_hint):
        started=time.monotonic()
        try: result=self.llm.json(prompt,schema_hint=schema_hint,temperature=0.7)
        except TypeError: result=self.llm.json(prompt)
        self.json_retry_total+=int(getattr(self.llm,"last_json_retry_count",0))
        self.llm_latency_total_ms+=float(getattr(self.llm,"last_latency_ms",(time.monotonic()-started)*1000))
        return result
    def rewrite(self,segment):
        self.rewrite_calls+=1
        result=self._ask(_rewrite_prompt(segment,self.variant_count),'{"candidates":["..."]}')
        candidates=result.get("candidates") if isinstance(result,dict) else None
        if not isinstance(candidates,list) or not all(isinstance(v,str) for v in candidates): raise SegmentFailure("schema_error","rewrite","model response needs candidates array")
        cleaned=list(dict.fromkeys(_clean_text(v) for v in candidates if _clean_text(v)))
        if not cleaned: raise SegmentFailure("empty_candidates","rewrite","model returned no usable candidate text")
        segment.candidates=cleaned; segment.strategy="direct_model"
    def review(self,segment,analysis=None):
        del analysis
        if not segment.candidates: raise SegmentFailure("empty_candidates","review",f"{segment.id}: no candidates")
        return {"candidate_count":len(segment.candidates),"strategy":"direct_model"}
    def process_one(self,segment,max_retries=2):
        last=None
        for attempt in range(max(0,max_retries)+1):
            try:
                if attempt: self.retry_calls+=1; segment.candidates=[]
                self.rewrite(segment); review=self.review(segment)
                return {"segment":_segment_to_json(segment),"review":review,"attempts":attempt+1,"analysis_attempts":0}
            except SegmentFailure as exc: last=exc
        if last: raise last
        raise SegmentFailure("unknown","rewrite",f"{segment.id}: unknown failure")
    def process(self,segments):
        return {"segments":[self.process_one(s)["segment"] for s in segments],"review":[]}
    def choose(self,segment,history=None):
        history=history or []; values=segment.candidates or segment.variants
        available=[v for v in values if v not in history] or values
        if not available: raise TimelineError(f"{segment.id}: no candidate")
        return self.random.choice(available)
    def process_batch(self,segments,max_retries=2,completed=None,checkpoint=None,progress=None):
        completed=completed or {}; processed={}; failures=[]; stats={"generalize_strategy_version":GENERALIZE_STRATEGY_VERSION,"rewrite_calls":0,"retry_calls":0,"json_retry_count":0,"failure_reasons":{}}
        for index,segment in enumerate(segments,1):
            previous=completed.get(segment.id)
            if previous and previous.get("generalize_strategy_version")==GENERALIZE_STRATEGY_VERSION and previous.get("segment_fingerprint")==_segment_fingerprint(segment):
                processed[segment.id]=previous
                if progress: progress(f"[{index}/{len(segments)}] {segment.id} RESUME")
                continue
            try:
                result=self.process_one(segment,max_retries=max_retries); output=result["segment"]; output.update({"generalize_status":"completed","status":"completed"}); processed[segment.id]=output
                if progress: progress(f"[{index}/{len(segments)}] {segment.id} PASS direct_model")
            except SegmentFailure as exc:
                output=_segment_to_json(segment); output.update({"generalize_status":"failed","status":"failed","fallback":"original","fallback_text":segment.original_text,"failure_reason":exc.reason}); processed[segment.id]=output
                failures.append({"segment_id":segment.id,"stage":exc.stage,"reason":exc.reason,"last_error":str(exc)})
                stats["failure_reasons"][exc.reason]=stats["failure_reasons"].get(exc.reason,0)+1
                if progress: progress(f"[{index}/{len(segments)}] {segment.id} FAIL {exc.reason}")
            stats.update({"rewrite_calls":self.rewrite_calls,"retry_calls":self.retry_calls,"json_retry_count":self.json_retry_total})
            if checkpoint: checkpoint({"segments":[processed[i.id] for i in segments if i.id in processed],"failed_segments":failures,"stats":stats})
        return {"segments":[processed[i.id] for i in segments],"total":len(segments),"success":len(segments)-len(failures),"failed":len(failures),"failed_segments":failures,"stats":stats}

def _segment_to_json(segment):
    data={"id":segment.id,"start":segment.start,"end":segment.end,"original_text":segment.original_text,"candidates":segment.candidates,"mode":"atomic","strategy":"direct_model","generalize_strategy_version":GENERALIZE_STRATEGY_VERSION,"segment_fingerprint":_segment_fingerprint(segment),"speech_end":segment.speech_end if segment.speech_end is not None else segment.end,"speech_duration":segment.target_duration,"pause_after":segment.pause_after,"timeline_duration":segment.end-segment.start,"fallback_text":segment.original_text}
    data.update(segment.metadata); return data
def _segment_fingerprint(segment):
    value=json.dumps({"id":segment.id,"start":segment.start,"end":segment.end,"speech_duration":segment.target_duration,"text":segment.original_text},ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()[:16]
def load_segments(path:Path):
    with path.open(encoding="utf-8") as h: raw=json.load(h)
    values=raw.get("segments") if isinstance(raw,dict) else raw
    if not isinstance(values,list) or not values: raise TimelineError("input must contain a non-empty segments array")
    segments=[]
    for index,value in enumerate(values,1):
        if not isinstance(value,dict): raise TimelineError("every segment must be an object")
        text=_clean_text(value.get("original_text",value.get("text")))
        if not text: raise TimelineError(f"segment {index} has empty text")
        try: start=float(value["start"]); end=float(value["end"])
        except (KeyError,TypeError,ValueError) as exc: raise TimelineError(f"segment {index} needs numeric start and end") from exc
        if start<0 or end<=start: raise TimelineError(f"segment {index} has invalid timestamps")
        speech_end=float(value.get("speech_end",end)); pause_after=float(value.get("pause_after",max(0.0,end-speech_end)))
        metadata={k:value[k] for k in ("parent_segment_id","source_segment_id","source_segment_ids","source_start","source_end") if k in value}
        segments.append(Segment(_clean_text(value.get("id")) or f"seg_{index:04d}",start,end,text,candidates=[_clean_text(x) for x in value.get("candidates",[]) if _clean_text(x)],metadata=metadata,speech_end=speech_end,pause_after=pause_after,strategy="direct_model"))
    return segments
def save_result(path:Path,result:Dict[str,Any],source:Path,model:str):
    output={"schema_version":1,"source":str(source),"model":model,"generalize_strategy_version":GENERALIZE_STRATEGY_VERSION,"segments":result["segments"]}
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")