"""④ pyannote 说话人区分，并把说话人标签贴到 ASR 句子上"""
import os

import numpy as np

from ..config import CFG, free_gpu
from .preprocess import SR


def diarize(wav: np.ndarray, num_speakers=None, min_speakers=None, max_speakers=None) -> list[dict]:
    import torch
    from pyannote.audio import Pipeline

    c = CFG["diarization"]
    token = os.getenv("HF_TOKEN") or None
    pipe = Pipeline.from_pretrained(c["model"], token=token)
    if pipe is None:
        raise RuntimeError("pyannote 模型加载失败：检查 HF_TOKEN 以及是否已在 HuggingFace 同意模型协议")
    if CFG["device"] == "cuda" and torch.cuda.is_available():
        pipe.to(torch.device("cuda"))

    kw = {k: v for k, v in {
        "num_speakers": num_speakers or c.get("num_speakers"),
        "min_speakers": min_speakers or c.get("min_speakers"),
        "max_speakers": max_speakers or c.get("max_speakers"),
    }.items() if v}
    out = pipe({"waveform": torch.from_numpy(wav).unsqueeze(0), "sample_rate": SR}, **kw)
    ann = (getattr(out, "exclusive_speaker_diarization", None)
           or getattr(out, "speaker_diarization", None) or out)
    turns = [{"start": round(t.start, 2), "end": round(t.end, 2), "speaker": spk}
             for t, _, spk in ann.itertracks(yield_label=True)]

    del pipe
    free_gpu()
    return turns


def assign_speakers(sentences: list[dict], turns: list[dict]) -> list[dict]:
    """每句取时间重叠最多的说话人；按出场顺序重命名为 说话人1/2/…"""
    names = {}
    for s in sentences:
        best, best_ov = None, 0.0
        for t in turns:
            ov = min(s["end"], t["end"]) - max(s["start"], t["start"])
            if ov > best_ov:
                best, best_ov = t["speaker"], ov
        if best is None and turns:  # 无重叠：取最近的
            mid = (s["start"] + s["end"]) / 2
            best = min(turns, key=lambda t: abs((t["start"] + t["end"]) / 2 - mid))["speaker"]
        if best is not None:
            names.setdefault(best, f"说话人{len(names) + 1}")
            s["speaker"] = names[best]
    return sentences
