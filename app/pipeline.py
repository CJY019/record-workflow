"""串联 ①~⑤，每步结果落盘到任务文件夹（见 LAYOUT），可从任意阶段重跑"""
import json
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import formats
from .config import CFG

STAGES = ["preprocess", "vad", "asr", "diarize", "summarize"]
_locks = {}
_locks_guard = threading.Lock()


def _file_lock(p: Path):
    with _locks_guard:
        return _locks.setdefault(str(p.resolve()).casefold(), threading.RLock())


def _rj(p: Path):
    with _file_lock(p):
        return json.loads(p.read_text(encoding="utf-8"))


def _write_text(p: Path, text: str):
    with _file_lock(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(p)
        finally:
            tmp.unlink(missing_ok=True)


def _wj(p: Path, obj):
    _write_text(p, json.dumps(obj, ensure_ascii=False, indent=2))


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
        # 进度、重命名和总结版本可能来自不同线程，合并最新字段再原子写盘。
        with _file_lock(self.meta_path):
            latest = _rj(self.meta_path) if self.meta_path.exists() else dict(self.meta)
            latest.update(kw)
            self.meta = latest
            self.save()

    def f(self, name: str) -> Path:
        if self.legacy:
            legacy = {"summaries.json": "summaries/index.json", "chats_md": "chats_md",
                      "materials_work": "materials_work", "materials.json": "attachments/index.json"}
            return self.dir / legacy.get(name, name)
        p = self.dir / LAYOUT.get(name, name)
        (p if name in DIR_KEYS else p.parent).mkdir(parents=True, exist_ok=True)
        return p


def _library_context(job: Job, transcript: str) -> str:
    """按本节课内容检索项目资料库（失败不影响总结）"""
    pid = job.meta.get("project")
    if not pid:
        return ""
    from . import library, materials

    try:
        library.build(pid)
        return library.context_text(pid, transcript, library._cfg()["summary_chars"], exclude_job=job.id,
                                    exclude_names={m["name"] for m in materials.list_all(job)})
    except Exception:
        traceback.print_exc()
        return ""


def _finish(job: Job):
    job.update(status="done", stage="done", progress=1.0, queue_lane=None,
               elapsed_sec=round(time.time() - (job.meta.get("started_at") or time.time()), 1))
    if job.meta.get("project"):
        from . import library

        try:
            library.refresh_async(job.meta["project"], min_interval=0)
        except Exception:
            traceback.print_exc()


def _fail(job: Job, exc: Exception):
    traceback.print_exc()
    job.update(status="error", error=f"{type(exc).__name__}: {exc}", queue_lane=None,
               elapsed_sec=round(time.time() - (job.meta.get("started_at") or time.time()), 1))


def run_local(job: Job, from_stage: str = "preprocess") -> bool:
    """串行使用本机模型，转录全部落盘后返回是否需要远程总结。"""
    from .stages import preprocess, vad, asr, diarize

    def stage(name, frac=0.0):
        job.update(stage=name, resume_from=name, progress=round(frac, 3))

    try:
        start = STAGES.index(from_stage)
        p = job.meta.get("params", {})
        job.update(status="running", queue_lane="local", error=None,
                   started_at=job.meta.get("started_at") or time.time())
        wav_path = job.f("audio.wav")
        if start <= 0:
            stage("preprocess")
            inputs = job.meta.get("inputs") or [{"file": job.meta["input"],
                                                 "name": job.meta.get("filename", "")}]
            job.update(parts=preprocess.concat_to_wav16k(
                [(job.f(x["file"]), x["name"]) for x in inputs], wav_path, wav_path.parent))
            job.update(resume_from="vad")
        wav = preprocess.load_wav(wav_path) if start <= 3 else None
        if wav is not None:
            job.update(duration_sec=round(len(wav) / preprocess.SR, 1))

        if start <= 1:
            stage("vad")
            _wj(job.f("segments.json"), vad.segment(wav))
            job.update(resume_from="asr")

        if start <= 2:
            stage("asr")
            res = asr.transcribe(wav, _rj(job.f("segments.json")), context=p.get("context", ""),
                                 language=p.get("language"),
                                 progress=lambda x: stage("asr", x))
            _wj(job.f("asr.json"), res)
            job.update(resume_from="diarize")

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
        _write_text(job.f("transcript.md"), formats.to_markdown(sentences, title, parts))
        _write_text(job.f("transcript.srt"), formats.to_srt(sentences))
        text = formats.to_text(sentences, parts)
        _write_text(job.f("transcript.txt"), text)
        job.update(transcript_ready_at=time.time())
        if p.get("summarize", True):
            job.update(resume_from="summarize")
            return True
        _finish(job)
    except Exception as exc:
        _fail(job, exc)
    return False


def run_summary(job: Job):
    """远程总结不加载音频、不占 GPU，可与后续录音的本地阶段同时执行。"""
    from . import llm, materials, summaries
    from .stages import summarize

    try:
        p = job.meta.get("params", {})
        if not p.get("summarize", True):
            _finish(job)
            return
        run_id = job.meta.get("run_id") or uuid.uuid4().hex
        job.update(status="running", stage="summarize", resume_from="summarize", queue_lane="summary",
                   progress=0, error=None, started_at=job.meta.get("started_at") or time.time(),
                   summary_started_at=time.time(), run_id=run_id, summary_attempts=[], summary_notice=None)
        previous = next((it for it in summaries.list_all(job)["items"] if it.get("run_id") == run_id), None)
        if previous:
            summaries.activate(job, previous["id"])
            _finish(job)
            return
        text = job.f("transcript.txt").read_text(encoding="utf-8")
        extra = p.get("extra", "")
        if p.get("context"):
            extra = f"背景信息：{p['context']}\n{extra}"
        job.update(summary_phase="materials")
        materials.wait_ready(job)
        mats = materials.context_parts(job, any(llm.supports_vision(m) for m in llm.summary_models(p.get("model"))))
        job.update(summary_phase="library")
        lib = _library_context(job, text)
        attempts, used = [], []

        def on_attempt(model, state, detail):
            if state == "calling":
                failed = next((a for a in reversed(attempts) if a["status"] == "failed"), None)
                attempts.append({"model": model, "status": state, "started_at": time.time()})
                notice = f"{llm.label(failed['model'])} 未完成，已切换到 {llm.label(model)}" if failed else None
            else:
                attempts[-1].update(status=state, detail=detail, finished_at=time.time())
                notice = f"{llm.label(model)} 未完成，准备尝试备用模型" if state == "failed" else job.meta.get("summary_notice")
            job.update(summary_phase="model", summary_active_model=model, summary_attempts=attempts, summary_notice=notice)

        def on_model(model):
            if model not in used:
                used.append(model)

        md = summarize.summarize(text, p.get("prompt"), p.get("model"), extra,
            progress=lambda x: job.update(progress=round(x, 3)), materials=mats, library=lib,
            on_attempt=on_attempt, on_model=on_model)
        title = job.meta.get("title") or Path(job.meta.get("filename", "录音")).stem
        actual = used[-1]
        summaries.add(job, f"# {title} · 总结\n\n{md}\n", actual,
                      p.get("prompt") or CFG["summary"]["default_prompt"], run_id=run_id, models_used=used)
        _finish(job)
    except Exception as exc:
        _fail(job, exc)


def run(job: Job, from_stage: str = "preprocess"):
    """CLI 保持同步；网页调度器将两个阶段分别送入各自的队列。"""
    job.update(run_id=uuid.uuid4().hex, started_at=None, elapsed_sec=None, dispatch_ready=True,
               resume_from=from_stage, error=None)
    if from_stage == "summarize" or run_local(job, from_stage):
        run_summary(job)
