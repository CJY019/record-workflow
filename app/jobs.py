"""任务注册表：本地阶段串行使用 GPU，远程总结独立限流并发。

任务文件夹可以在任何位置：未归类/未关联文件夹的项目放在 data/jobs/<id>，
关联了文件夹的项目放在 <项目文件夹>/<标题>/。data/registry.json 记录 id → 路径。
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path

from .config import CFG, JOBS_DIR
from .pipeline import STAGES, Job, _rj, _wj, is_job_dir, run_local, run_summary

_local_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
_summary_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
_started = False
_workers = []
_stop = threading.Event()
_scheduler_lock = threading.RLock()
_scheduled: dict[str, dict] = {}
_REG = JOBS_DIR.parent / "registry.json"
_reg_lock = threading.Lock()


# ---------- 注册表 ----------
def _load_reg() -> dict:
    reg = json.loads(_REG.read_text(encoding="utf-8")) if _REG.exists() else {}
    changed = False
    for d in JOBS_DIR.iterdir():  # 收录 data/jobs 下的任务（含旧版）
        if d.is_dir() and is_job_dir(d):
            try:
                jid = Job(d).id
            except (OSError, ValueError):
                traceback.print_exc()
                continue
            if jid not in reg:
                reg[jid] = str(d)
                changed = True
    if changed:
        _save_reg(reg)
    return reg


def _save_reg(reg: dict):
    _wj(_REG, reg)


def get(job_id: str) -> Job | None:
    with _reg_lock:
        path = _load_reg().get(job_id)
    if not path or not is_job_dir(Path(path)):
        return None
    return Job(Path(path))


def list_all() -> list[dict]:
    with _reg_lock:
        reg = _load_reg()
    out = []
    for jid, path in reg.items():
        if is_job_dir(Path(path)):
            try:
                out.append(Job(Path(path)).meta | {"dir": path})
            except (OSError, ValueError):
                traceback.print_exc()
    positions = queue_positions()
    for meta in out:
        meta.update(positions.get(meta["id"], {}))
    return sorted(out, key=lambda m: m.get("created_at", 0), reverse=True)


def unregister(job_id: str):
    with _reg_lock:
        reg = _load_reg()
        reg.pop(job_id, None)
        _save_reg(reg)


def _safe(name: str) -> str:
    s = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name).strip(" .")
    return s[:80] or "录音"


def _unique(parent: Path, name: str, exclude: Path | None = None) -> Path:
    p, i = parent / name, 2
    while p.exists() and p != exclude:
        p, i = parent / f"{name} ({i})", i + 1
    return p


def home_dir(project_id: str | None, title: str, job_id: str, exclude: Path | None = None) -> Path:
    """任务应在的位置：项目文件夹/<标题>，否则 data/jobs/<id>"""
    from . import projects

    p = projects.get(project_id) if project_id else None
    if p and p.get("folder") and Path(p["folder"]).is_dir():
        return _unique(Path(p["folder"]), _safe(title), exclude)
    return JOBS_DIR / job_id


def _hide(p: Path):
    if os.name == "nt" and p.exists():
        subprocess.run(["attrib", "+h", str(p)], capture_output=True)


def relocate(job: Job) -> Job:
    """按当前项目/标题把任务文件夹移动到应在的位置"""
    with _scheduler_lock:
        job = get(job.id) or job
        if is_busy(job):
            raise RuntimeError("任务正在处理，完成后才能移动")
        return _relocate(job)


def _relocate(job: Job) -> Job:
    from . import materials

    if materials.pending(job):
        raise RuntimeError("课件仍在转换，完成后才能移动")
    title = job.meta.get("title") or Path(job.meta.get("filename", "录音")).stem
    target = home_dir(job.meta.get("project"), title, job.id, exclude=job.dir)
    if target.resolve() == job.dir.resolve():
        return job
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(job.dir), str(target))
    with _reg_lock:
        reg = _load_reg()
        reg[job.id] = str(target)
        _save_reg(reg)
    return Job(target)


# ---------- 旧版平铺结构 → 新版分类结构 ----------
def upgrade_legacy(job: Job) -> Job:
    if not job.legacy or job.meta.get("status") in ("running", "queued"):
        return job
    d = job.dir
    tmp = d.with_name(d.name + ".upgrading")
    shutil.move(str(d), str(tmp))
    d.mkdir()
    try:
        return _upgrade(d, tmp)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)  # 失败则原样恢复
        shutil.move(str(tmp), str(d))
        raise


def _upgrade(d: Path, tmp: Path) -> Job:
    new = Job(d)  # 空目录 → 新版布局
    old = lambda n: tmp / n  # noqa: E731
    meta = json.loads(old("meta.json").read_text(encoding="utf-8"))
    # 原始录音
    inputs = meta.get("inputs") or [{"file": meta.get("input"), "name": meta.get("filename", "录音")}]
    for x in inputs:
        if x.get("file") and old(x["file"]).exists():
            dst = new.f("inputs") / _safe(Path(x["name"]).name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old(x["file"])), dst)
            x["file"] = str(dst.relative_to(d)).replace("\\", "/")
    meta["inputs"] = inputs
    meta.pop("input", None)
    for n in ["audio.wav", "segments.json", "asr.json", "diar.json", "transcript.json",
              "transcript.md", "transcript.srt", "transcript.txt", "summary.md"]:
        if old(n).exists():
            shutil.move(str(old(n)), new.f(n))
    # 总结版本：summaries/<sid>.md + index.json
    if old("summaries/index.json").exists():
        idx = json.loads(old("summaries/index.json").read_text(encoding="utf-8"))
        for it in idx["items"]:
            src = old(f"summaries/{it.get('file') or it['id'] + '.md'}")
            name = _safe(it["title"]) + ".md"
            if src.exists():
                shutil.move(str(src), new.f("summaries") / name)
            it["file"] = name
        new.f("summaries.json").write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    # 问答
    if old("chats").is_dir():
        for f in old("chats").glob("*.json"):
            shutil.move(str(f), new.f("chats") / f.name)
    new.meta = meta
    new.save()
    _hide(d / ".work")
    shutil.rmtree(tmp)
    from . import chat

    for c in chat.list_convs(new):  # 生成可读的 问答记录/*.md
        chat._save(new, chat.get(new, c["id"]))
    return new


# ---------- 队列 ----------
def _queue_job(job: Job, from_stage: str, *, recovering: bool = False):
    lane = "summary" if from_stage == "summarize" else "local"
    queued_at = (job.meta.get("queued_at") or job.meta.get("created_at") or time.time()) if recovering else time.time()
    job.update(status="queued", stage=from_stage, resume_from=from_stage, queue_lane=lane,
               queued_at=queued_at, dispatch_ready=True, error=None,
               run_id=job.meta.get("run_id") or uuid.uuid4().hex)
    _scheduled[job.id] = {"queue_lane": lane, "state": "queued", "queued_at": queued_at}
    (_summary_q if lane == "summary" else _local_q).put((job.id, from_stage))


def _worker(lane: str):
    work = _summary_q if lane == "summary" else _local_q
    while not _stop.is_set():
        try:
            job_id, from_stage = work.get(timeout=.2)
        except queue.Empty:
            continue
        transferred, job = False, None
        try:
            if _stop.is_set():
                continue
            with _scheduler_lock:
                _scheduled[job_id]["state"] = "running"
            job = get(job_id)
            if not job:
                continue
            if lane == "summary":
                run_summary(job)
            elif run_local(job, from_stage):
                with _scheduler_lock:
                    _queue_job(job, "summarize")
                    transferred = True
        except Exception as exc:
            traceback.print_exc()
            if job:
                try:
                    job.update(status="error", error=f"{type(exc).__name__}: {exc}", queue_lane=None)
                except Exception:
                    traceback.print_exc()
        finally:
            if not transferred:
                with _scheduler_lock:
                    _scheduled.pop(job_id, None)
            work.task_done()


def _resume_stage(job: Job) -> str:
    """以提交过的检查点为准；缺少前置产物时向前退，不能跳过用户指定的重跑。"""
    stage = job.meta.get("resume_from") or job.meta.get("stage") or "preprocess"
    if stage not in STAGES:
        stage = "preprocess"
    index = STAGES.index(stage)
    if index == 4:
        try:
            transcript = _rj(job.f("transcript.json"))
            if isinstance(transcript.get("sentences"), list) and job.f("transcript.txt").exists():
                return "summarize"
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        index = 3
    if index >= 3:
        try:
            if not isinstance(_rj(job.f("asr.json"))["sentences"], list):
                index = 2
        except (OSError, ValueError, TypeError, KeyError):
            index = 2
    if index >= 2:
        try:
            if not isinstance(_rj(job.f("segments.json")), list):
                index = 1
        except (OSError, ValueError, TypeError):
            index = 1
    if index >= 1:
        import wave

        try:
            with wave.open(str(job.f("audio.wav")), "rb") as wav:
                if wav.getframerate() != 16000 or wav.getnchannels() != 1:
                    index = 0
        except (OSError, EOFError, wave.Error):
            index = 0
    return STAGES[index]


def start_worker(recover: bool = True):
    global _started
    with _scheduler_lock:
        if _started:
            return
        _started = True
        _stop.clear()
        if recover:
            items = sorted(list_all(), key=lambda m: m.get("queued_at") or m.get("created_at", 0))
            for m in items:
                try:
                    job = get(m["id"])
                    from . import materials

                    materials.resume_pending(job)
                    if m.get("status") in ("running", "queued"):
                        if m.get("dispatch_ready") is False:
                            job.update(status="error", error="任务在提交完成前中断，请重新上传")
                        else:
                            _queue_job(job, _resume_stage(job), recovering=True)
                    else:
                        if job.legacy:
                            job = upgrade_legacy(job)
                        relocate(job)
                except Exception:
                    traceback.print_exc()
        count = max(1, min(4, int((CFG.get("pipeline") or {}).get("summary_workers", 2))))
        for lane in ["local", *(["summary"] * count)]:
            thread = threading.Thread(target=_worker, args=(lane,), daemon=True, name=f"notes-{lane}")
            _workers.append(thread)
            thread.start()


def stop_workers(timeout: float = 5):
    """停止领取新任务；未完成的阶段由下一次启动按检查点恢复。"""
    global _started
    _stop.set()
    deadline = time.monotonic() + timeout
    for thread in _workers:
        thread.join(max(0, deadline - time.monotonic()))
    if not any(thread.is_alive() for thread in _workers):
        with _scheduler_lock:
            _started = False
            _workers.clear()
            _scheduled.clear()
            for work in (_local_q, _summary_q):
                while True:
                    try:
                        work.get_nowait(); work.task_done()
                    except queue.Empty:
                        break


def is_busy(job: Job) -> bool:
    with _scheduler_lock:
        return job.id in _scheduled or job.meta.get("status") in ("running", "queued")


def queue_positions() -> dict:
    with _scheduler_lock:
        result = {}
        for lane in ("local", "summary"):
            waiting = [(jid, item) for jid, item in _scheduled.items() if item["queue_lane"] == lane and item["state"] == "queued"]
            for position, (jid, item) in enumerate(sorted(waiting, key=lambda pair: pair[1]["queued_at"]), 1):
                result[jid] = {"queue_lane": lane, "queue_position": position}
        return result


def queue_status() -> dict:
    with _scheduler_lock:
        return {lane: {state: sum(item["queue_lane"] == lane and item["state"] == state for item in _scheduled.values())
                       for state in ("queued", "running")} for lane in ("local", "summary")}


def _title(names: list[str]) -> str:
    """多个文件合并时取公共前缀作标题，如 课程2-1/2-2 → 课程2"""
    stems = [Path(n).stem for n in names]
    if len(stems) == 1:
        return names[0]
    common = os.path.commonprefix(stems).rstrip("-_ .（(第")
    return common or f"{stems[0]} 等{len(stems)}个"


def create(files: list[tuple[Path, str]], params: dict, enqueue: bool = True,
           project: str | None = None) -> Job:
    """files: [(临时文件路径, 原文件名)]；多个文件会按顺序拼接为一个任务"""
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    filename = _title([n for _, n in files])
    d = home_dir(project, Path(filename).stem, job_id)
    d.mkdir(parents=True)
    job = Job(d)
    inputs = []
    for src, name in files:
        dst = job.f("inputs") / _safe(Path(name).name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        i = 2
        while dst.exists():
            dst = dst.with_name(f"{Path(name).stem} ({i}){Path(name).suffix}")
            i += 1
        shutil.move(str(src), dst)
        inputs.append({"file": str(dst.relative_to(d)).replace("\\", "/"), "name": name})
    job.meta = {"id": job_id, "filename": filename, "inputs": inputs, "project": project,
                "params": params, "status": "queued", "stage": None, "progress": 0,
                "created_at": time.time(), "dispatch_ready": False}
    job.save()
    _hide(d / ".work")
    with _reg_lock:
        reg = _load_reg()
        reg[job_id] = str(d)
        _save_reg(reg)
    if enqueue:
        _schedule(job_id, "preprocess")
    return job


def enqueue(job_id: str, from_stage: str = "preprocess"):
    return _schedule(job_id, from_stage)


def _schedule(job_id: str, from_stage: str):
    if from_stage not in STAGES:
        raise ValueError(f"未知处理阶段：{from_stage}")
    with _scheduler_lock:
        if job_id in _scheduled:
            return False
        job = get(job_id)
        if not job:
            raise KeyError(job_id)
        _queue_job(job, from_stage)
        return True


def rerun(job: Job, from_stage: str, params: dict):
    if from_stage not in STAGES:
        raise ValueError(f"未知处理阶段：{from_stage}")
    with _scheduler_lock:
        job = get(job.id) or job
        if is_busy(job):
            raise RuntimeError("任务已经在处理或排队中")
        updated = {**job.meta.get("params", {}), **{k: v for k, v in params.items() if v is not None}}
        job.update(params=updated, progress=0, started_at=None, elapsed_sec=None, run_id=uuid.uuid4().hex)
        _schedule(job.id, from_stage)


def patch_job(job: Job, *, title: str | None = None, project: str | None = None) -> Job:
    """项目变更与入队共用调度锁，拒绝移动时不先修改逻辑项目。"""
    with _scheduler_lock:
        job = get(job.id) or job
        busy = is_busy(job)
        from . import materials, projects

        if project is not None:
            if busy or materials.pending(job):
                raise RuntimeError("任务或课件正在处理，完成后才能移动")
            if project and not projects.get(project):
                raise ValueError("项目不存在")
        fields = {}
        if title is not None:
            fields["title"] = title.strip() or None
        if project is not None:
            fields["project"] = project or None
        if fields:
            job.update(**fields)
        if not busy and not materials.pending(job):
            job = _relocate(job)
        return job


def delete(job: Job):
    with _scheduler_lock:
        job = get(job.id) or job
        if is_busy(job):
            raise RuntimeError("任务正在处理或排队，完成后才能删除")
        from . import materials

        if materials.pending(job):
            raise RuntimeError("课件仍在转换，完成后才能删除")
        if not is_job_dir(job.dir):
            raise RuntimeError("不是任务文件夹，拒绝删除")
        shutil.rmtree(job.dir)
        unregister(job.id)


def queue_size() -> int:
    return sum(lane["queued"] for lane in queue_status().values())
