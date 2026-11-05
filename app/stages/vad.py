"""② silero-vad 检测语音区间，合并成 ≤ max_segment_sec 的段"""
import numpy as np

from ..config import CFG
from .preprocess import SR

_model = None


def _get_model():
    global _model
    if _model is None:
        from silero_vad import load_silero_vad

        _model = load_silero_vad()
    return _model


def segment(wav: np.ndarray) -> list[dict]:
    """返回 [{start, end}]（秒）"""
    import torch
    from silero_vad import get_speech_timestamps

    c = CFG["vad"]
    speech = get_speech_timestamps(
        torch.from_numpy(wav), _get_model(), sampling_rate=SR,
        threshold=c["threshold"], min_silence_duration_ms=c["min_silence_ms"],
        speech_pad_ms=200,
    )
    max_len = int(c["max_segment_sec"] * SR)

    # 过长的语音区间先硬切
    pieces = []
    for s in speech:
        a, b = s["start"], s["end"]
        while b - a > max_len:
            pieces.append((a, a + max_len))
            a += max_len
        pieces.append((a, b))

    # 相邻区间贪心合并
    merged = []
    for a, b in pieces:
        if merged and b - merged[-1][0] <= max_len:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [{"start": round(a / SR, 3), "end": round(b / SR, 3)} for a, b in merged]
