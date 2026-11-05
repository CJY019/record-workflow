"""任务注册表 + 单 worker 队列（GPU 同一时间只跑一个任务）

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
import uuid
from pathlib import Path

from .config import JOBS_DIR
from .pipeline import Job, is_job_dir, run

_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
_started = False
_REG = JOBS_DIR.parent / "registry.json"
_reg_lock = threading.Lock()


# ---------- 注册表 ----------
def _load_reg() -> dict:
    reg = json.loads(_REG.read_text(encoding="utf-8")) if _REG.exists() else {}
    changed = False
    for d in JOBS_DIR.iterdir():  # 收录 data/jobs 下的任务（含旧版）
        if d.is_dir() and is_job_dir(d):
            jid = Job(d).id
            if jid not in reg:
                reg[jid] = str(d)
                changed = True
    if changed:
        _save_reg(reg)
    return reg


def _save_reg(reg: dict):
    _REG.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


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
            out.append(Job(Path(path)).meta | {"dir": path})
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
    if job.meta.get("status") in ("running", "queued"):
        raise RuntimeError("任务正在处理，完成后才能移动")
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
def _worker():
    while True:
        job_id, from_stage = _q.get()
        try:
            j = get(job_id)
            if j:
                run(j, from_stage)
        finally:
            _q.task_done()


def start_worker():
    global _started
    if not _started:
        threading.Thread(target=_worker, daemon=True).start()
        _started = True
        for m in list_all():  # 服务重启时，把中断的任务标记为失败
            if m.get("status") in ("running", "queued"):
                get(m["id"]).update(status="error", error="服务重启，任务中断；可在「更多」里重跑")
        for m in list_all():  # 旧任务升级为分类目录结构，并放到所属项目文件夹
            try:
                j = get(m["id"])
                if j.legacy:
                    j = upgrade_legacy(j)
                relocate(j)
            except Exception as e:
                print("整理任务目录失败", m.get("id"), e)


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
                "created_at": time.time()}
    job.save()
    _hide(d / ".work")
    with _reg_lock:
        reg = _load_reg()
        reg[job_id] = str(d)
        _save_reg(reg)
    if enqueue:
        _q.put((job_id, "preprocess"))
    return job


def enqueue(job_id: str, from_stage: str = "preprocess"):
    _q.put((job_id, from_stage))


def rerun(job: Job, from_stage: str, params: dict):
    job.meta["params"].update({k: v for k, v in params.items() if v is not None})
    job.update(status="queued", progress=0)
    _q.put((job.id, from_stage))


def delete(job: Job):
    if not is_job_dir(job.dir):
        raise RuntimeError("不是任务文件夹，拒绝删除")
    shutil.rmtree(job.dir)
    unregister(job.id)


def queue_size() -> int:
    return _q.qsize()
