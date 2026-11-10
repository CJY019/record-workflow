"""总结的多版本管理：总结/<版本名>.md（索引在 .work/summaries.json）；笔记.md 始终是当前选中版本的副本"""
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from .pipeline import Job, _wj, _write_text

_lock = threading.Lock()


def _dir(job: Job):
    d = job.f("summaries")
    d.mkdir(exist_ok=True)
    return d


BAD = '<>:"/\\|?*'


def _safe(name: str) -> str:
    return "".join("_" if c in BAD else c for c in name).strip(" .")[:60] or "总结"


def _file(job: Job, it: dict) -> Path:
    return _dir(job) / it.get("file", f"{it['id']}.md")


def _new_file(job: Job, title: str, keep: str | None = None) -> str:
    base, i = _safe(title), 2
    name = f"{base}.md"
    while (_dir(job) / name).exists() and name != keep:
        name, i = f"{base} ({i}).md", i + 1
    return name


def _index(job: Job) -> dict:
    p = job.f("summaries.json")
    idx = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"active": None, "items": []}
    # 迁移：旧任务只有 summary.md
    if not idx["items"] and job.f("summary.md").exists():
        sid = uuid.uuid4().hex[:8]
        shutil.copy(job.f("summary.md"), _dir(job) / f"{sid}.md")
        idx = {"active": sid, "items": [{
            "id": sid, "title": "总结 1", "file": f"{sid}.md", "model": job.meta.get("summary_model"),
            "prompt": job.meta.get("summary_prompt"), "created_at": job.f("summary.md").stat().st_mtime}]}
        _write(job, idx)
    return idx


def _write(job: Job, idx: dict):
    _wj(job.f("summaries.json"), idx)


def _sync_active(job: Job, idx: dict):
    if idx["active"]:
        it = next(i for i in idx["items"] if i["id"] == idx["active"])
        _write_text(job.f("summary.md"), _file(job, it).read_text(encoding="utf-8"))
        job.update(summary_model=it.get("model"), summary_prompt=it.get("prompt"))
    else:
        job.f("summary.md").unlink(missing_ok=True)
        job.update(summary_model=None, summary_prompt=None)


def list_all(job: Job) -> dict:
    with _lock:
        return _index(job)


def add(job: Job, text: str, model: str, prompt: str, *, run_id: str | None = None,
        models_used: list[str] | None = None) -> dict:
    """新增一个版本并设为当前"""
    with _lock:
        idx = _index(job)
        existing = next((it for it in idx["items"] if run_id and it.get("run_id") == run_id), None)
        if existing:
            return existing
        sid = uuid.uuid4().hex[:8]
        from . import llm

        try:
            mname = llm.label(model)
        except Exception:
            mname = model
        title = f"总结 {len(idx['items']) + 1}（{mname}）"
        fname = _new_file(job, title)
        _write_text(_dir(job) / fname, text)
        item = {"id": sid, "title": title, "file": fname, "model": model, "prompt": prompt,
                "created_at": time.time(), "run_id": run_id, "models_used": models_used or [model]}
        idx["items"].append(item)
        idx["active"] = sid
        _write(job, idx)
        _sync_active(job, idx)
        return item


def read(job: Job, sid: str) -> str:
    it = next((i for i in _index(job)["items"] if i["id"] == sid), None)
    if not it or not _file(job, it).exists():
        raise KeyError(sid)
    return _file(job, it).read_text(encoding="utf-8")


def rename(job: Job, sid: str, title: str) -> dict:
    with _lock:
        idx = _index(job)
        it = next((i for i in idx["items"] if i["id"] == sid), None)
        if not it:
            raise KeyError(sid)
        it["title"] = title.strip() or it["title"]
        new = _new_file(job, it["title"], keep=it.get("file"))
        if new != it.get("file") and _file(job, it).exists():
            _file(job, it).rename(_dir(job) / new)
            it["file"] = new
        _write(job, idx)
        return it


def activate(job: Job, sid: str):
    with _lock:
        idx = _index(job)
        if not any(i["id"] == sid for i in idx["items"]):
            raise KeyError(sid)
        idx["active"] = sid
        _write(job, idx)
        _sync_active(job, idx)


def delete(job: Job, sid: str):
    with _lock:
        idx = _index(job)
        for it in [i for i in idx["items"] if i["id"] == sid]:
            _file(job, it).unlink(missing_ok=True)
        idx["items"] = [i for i in idx["items"] if i["id"] != sid]
        if idx["active"] == sid:
            idx["active"] = idx["items"][-1]["id"] if idx["items"] else None
        _write(job, idx)
        _sync_active(job, idx)
