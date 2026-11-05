"""③ Qwen3-ASR 转录（+ ForcedAligner 时间戳），输出句子列表"""
import re
import threading
from pathlib import Path

import numpy as np

from ..config import CFG, MODELS_DIR, free_gpu
from .preprocess import SR

_model = None
_lock = threading.Lock()


def ensure_model(repo: str) -> str:
    """模型不存在时从 ModelScope 下载到 models/，返回本地路径"""
    local = MODELS_DIR / repo.replace("/", "__")
    if (local / "config.json").exists():
        return str(local)
    if Path(repo).exists():
        return repo
    from modelscope import snapshot_download

    print(f"[asr] 下载模型 {repo} → {local}")
    snapshot_download(repo, local_dir=str(local))
    return str(local)


def load():
    global _model
    with _lock:
        if _model is not None:
            return _model
        import torch
        from qwen_asr import Qwen3ASRModel

        c = CFG["asr"]
        dtype = getattr(torch, c["dtype"])
        device = "cuda:0" if CFG["device"] == "cuda" and torch.cuda.is_available() else "cpu"
        if device == "cpu":
            dtype = torch.float32
        kw = dict(dtype=dtype, device_map=device)
        _model = Qwen3ASRModel.from_pretrained(
            ensure_model(c["model"]),
            forced_aligner=ensure_model(c["aligner"]) if c.get("aligner") else None,
            forced_aligner_kwargs=kw,
            max_inference_batch_size=c["batch_size"],
            max_new_tokens=c["max_new_tokens"],
            **kw,
        )
        return _model


def unload():
    global _model
    _model = None
    free_gpu()


_SENT_END = "。！？!?；;…"


def _split_sentences(text: str, max_len: int = 80) -> list[tuple[int, int]]:
    """按句末标点切分，过长句子再按逗号切；返回字符区间"""
    spans, start = [], 0
    for m in re.finditer(f"[{_SENT_END}]+", text):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    out = []
    for a, b in spans:
        if b - a <= max_len:
            out.append((a, b))
            continue
        cur = a
        for m in re.finditer(r"[，,、：:]", text[a:b]):
            end = a + m.end()
            if end - cur >= max_len // 2:
                out.append((cur, end))
                cur = end
        if cur < b:
            out.append((cur, b))
    return [(a, b) for a, b in out if text[a:b].strip()]


def _sentences_from_chunk(text: str, items, seg_start: float, seg_end: float) -> list[dict]:
    if not text.strip():
        return []
    if not items:
        return [{"start": seg_start, "end": seg_end, "text": text.strip()}]

    # 把对齐 token 定位回原文字符位置
    low = text.lower()
    pos, cursor = [], 0
    for it in items:
        tok = (it.text or "").lower()
        if not tok:
            continue
        idx = low.find(tok, cursor)
        if idx < 0:
            continue
        pos.append((idx, it.start_time, it.end_time))
        cursor = idx + len(tok)

    sents = []
    for a, b in _split_sentences(text):
        hits = [(s, e) for i, s, e in pos if a <= i < b]
        if hits:
            st, en = seg_start + hits[0][0], seg_start + hits[-1][1]
        else:  # 无对齐结果：按字符比例估算
            span = seg_end - seg_start
            st = seg_start + span * a / len(text)
            en = seg_start + span * b / len(text)
        sents.append({"start": round(st, 2), "end": round(en, 2), "text": text[a:b].strip()})
    return sents


def transcribe(wav: np.ndarray, segments: list[dict], context: str = "", language=None,
               progress=None) -> dict:
    model = load()
    c = CFG["asr"]
    language = language if language is not None else c.get("language")
    use_ts = model.forced_aligner is not None
    bs = max(1, c["batch_size"])

    sentences, langs = [], []
    for i in range(0, len(segments), bs):
        batch = segments[i:i + bs]
        audios = [(wav[int(s["start"] * SR):int(s["end"] * SR)], SR) for s in batch]
        results = model.transcribe(audio=audios, context=context or c.get("default_context", ""),
                                   language=language or None, return_time_stamps=use_ts)
        for seg, r in zip(batch, results):
            items = list(r.time_stamps.items) if (use_ts and r.time_stamps) else []
            sentences += _sentences_from_chunk(r.text, items, seg["start"], seg["end"])
            if r.language:
                langs.append(r.language)
        if progress:
            progress(min(1.0, (i + bs) / len(segments)))

    del model, results  # 先释放局部引用，否则 unload 后显存仍被占用
    if CFG.get("unload_after_use"):
        unload()
    return {"language": max(set(langs), key=langs.count) if langs else "", "sentences": sentences}
