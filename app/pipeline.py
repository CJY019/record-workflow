"""串联 ①~⑤，每步结果落盘到任务文件夹（见 LAYOUT），可从任意阶段重跑"""
import json
import time
import traceback
from pathlib import Path

from . import formats
from .config import CFG

STAGES = ["preprocess", "vad", "asr", "diarize", "summarize"]


def _rj(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _wj(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# 新版目录结构：逻辑文件名 → 任务文件夹内的实际位置（人能看懂的分类目录 + 隐藏的 .work）
LAYOUT = {
    "meta.json": ".work/meta.json",
    "audio.wav": ".work/audio.wav",
    "segments.json": ".work/segments.json",
    "asr.json": ".work/asr.json",
    "diar.json": ".work/diar.json",
    "transcript.json": ".work/transcript.json",
    "transcript.md": "转录稿/转录稿.md",
    "transcript.srt": "转录稿/字幕.srt",
    "transcript.txt": "转录稿/转录稿.txt",
    "summary.md": "笔记.md",
    "summaries": "总结",
    "summaries.json": ".work/summaries.json",
    "chats": ".work/chats",
    "chats_md": "问答记录",
    "attachments": "课件资料",
    "materials_work": ".work/materials",
    "materials.json": ".work/materials.json",
    "inputs": "原始录音",
}


DIR_KEYS = {"summaries", "chats", "chats_md", "attachments", "materials_work", "inputs"}


def is_job_dir(d: Path) -> bool:
    return (d / ".work" / "meta.json").exists() or (d / "meta.json").exists()


class Job:
    def __init__(self, job_dir: Path):
        self.dir = Path(job_dir)
        # 旧任务（meta.json 在根目录）保持原有平铺结构
        self.legacy = (self.dir / "meta.json").exists() and not (self.dir / ".work").exists()
        self.meta_path = self.f("meta.json")
        self.meta = _rj(self.meta_path) if self.meta_path.exists() else {}

    @property
    def id(self):
        return self.meta.get("id") or self.dir.name

    def save(self):
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        _wj(self.meta_path, self.meta)

    def update(self, **kw):
        self.meta.update(kw)
        self.save()

    def f(self, name: str) -> Path:
        if self.legacy:
            legacy = {"summaries.json": "summaries/index.json", "chats_md": "chats_md",
                      "materials_work": "materials_work", "materials.json": "attachments/index.json"}
            return self.dir / legacy.get(name, name)
        p = self.dir / LAYOUT.get(name, name)
        (p if name in DIR_KEYS else p.parent).mkdir(parents=True, exist_ok=True)
        return p


def run(job: Job, from_stage: str = "preprocess"):
    """params 存在 job.meta['params']: diarize, language, context, prompt, model, num_speakers…"""
    from .stages import preprocess, vad, asr, diarize, summarize

    p = job.meta.get("params", {})
    start = STAGES.index(from_stage)
    t0 = time.time()
    job.update(status="running", error=None, started_at=t0)

    def stage(name, frac=0.0):
        job.update(stage=name, progress=round(frac, 3))

    try:
        wav_path = job.f("audio.wav")
        if start <= 0:
            stage("preprocess")
            inputs = job.meta.get("inputs") or [{"file": job.meta["input"],
                                                 "name": job.meta.get("filename", "")}]
            job.update(parts=preprocess.concat_to_wav16k(
                [(job.f(x["file"]), x["name"]) for x in inputs], wav_path, wav_path.parent))
        wav = preprocess.load_wav(wav_path) if start <= 3 else None
        if wav is not None:
            job.update(duration_sec=round(len(wav) / preprocess.SR, 1))

        if start <= 1:
            stage("vad")
            _wj(job.f("segments.json"), vad.segment(wav))

        if start <= 2:
            stage("asr")
            res = asr.transcribe(wav, _rj(job.f("segments.json")), context=p.get("context", ""),
                                 language=p.get("language"),
                                 progress=lambda x: stage("asr", x))
            _wj(job.f("asr.json"), res)

        res = _rj(job.f("asr.json"))
        sentences = res["sentences"]
        do_diar = p.get("diarize")
        if do_diar is None:
            do_diar = CFG["diarization"]["default"]

        if start <= 3:
            if do_diar:
                stage("diarize")
                turns = diarize.diarize(wav, p.get("num_speakers"), p.get("min_speakers"),
                                        p.get("max_speakers"))
                _wj(job.f("diar.json"), turns)
            elif job.f("diar.json").exists():
                job.f("diar.json").unlink()
        if do_diar and job.f("diar.json").exists():
            sentences = diarize.assign_speakers(sentences, _rj(job.f("diar.json")))

        title = job.meta.get("title") or Path(job.meta.get("filename", "录音")).stem
        parts = job.meta.get("parts") or []
        _wj(job.f("transcript.json"), {"language": res["language"], "parts": parts,
                                       "sentences": sentences})
        job.f("transcript.md").write_text(formats.to_markdown(sentences, title, parts),
                                          encoding="utf-8")
        job.f("transcript.srt").write_text(formats.to_srt(sentences), encoding="utf-8")
        text = formats.to_text(sentences, parts)
        job.f("transcript.txt").write_text(text, encoding="utf-8")

        if p.get("summarize", True):
            stage("summarize")
            extra = p.get("extra", "")
            if p.get("context"):
                extra = f"背景信息：{p['context']}\n{extra}"
            from . import llm, materials

            materials.wait_ready(job)  # 课件还在转换时等待
            mats = materials.context_parts(job, llm.supports_vision(p.get("model")))
            md = summarize.summarize(text, p.get("prompt"), p.get("model"), extra,
                                     progress=lambda x: stage("summarize", x), materials=mats)
            from . import summaries  # 每次生成保存为新版本，不覆盖旧总结

            summaries.add(job, f"# {title} · 总结\n\n{md}\n",
                          p.get("model") or CFG["summary"]["model"],
                          p.get("prompt") or CFG["summary"]["default_prompt"])

        job.update(status="done", stage="done", progress=1.0,
                   elapsed_sec=round(time.time() - t0, 1))
    except Exception as e:
        traceback.print_exc()
        job.update(status="error", error=f"{type(e).__name__}: {e}",
                   elapsed_sec=round(time.time() - t0, 1))
