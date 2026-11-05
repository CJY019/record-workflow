"""① 任意音视频 → 16kHz 单声道 wav"""
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 16000


def to_wav16k(src: Path, dst: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，请安装并加入 PATH")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 转码失败: {r.stderr.strip()}")
    return dst


def concat_to_wav16k(srcs: list[tuple[Path, str]], dst: Path, work_dir: Path) -> list[dict]:
    """逐个转码后按顺序拼接；返回各分段在总音频中的起始时间"""
    if len(srcs) == 1:
        to_wav16k(srcs[0][0], dst)
        return [{"name": srcs[0][1], "start": 0.0}]
    parts, wavs, t = [], [], 0.0
    for i, (src, name) in enumerate(srcs):
        tmp = work_dir / f"_part{i}.wav"
        wav = load_wav(to_wav16k(src, tmp))
        tmp.unlink()
        parts.append({"name": name, "start": round(t, 3)})
        wavs.append(wav)
        t += len(wav) / SR
    sf.write(str(dst), np.concatenate(wavs), SR, subtype="PCM_16")
    return parts


def load_wav(path: Path) -> np.ndarray:
    wav, sr = sf.read(str(path), dtype="float32")
    assert sr == SR, f"expected {SR}Hz, got {sr}"
    return wav
