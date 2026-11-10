"""项目（课程）管理：只做逻辑分组，存于 data/projects.json；任务通过 meta['project'] 归属"""
import json
import shutil
import threading
from pathlib import Path
import time
import uuid

from .config import JOBS_DIR

_FILE = JOBS_DIR.parent / "projects.json"
_lock = threading.Lock()


def _load() -> list[dict]:
    return json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []


def _save(items: list[dict]):
    _FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_all() -> list[dict]:
    return _load()


def get(pid: str) -> dict | None:
    return next((p for p in _load() if p["id"] == pid), None)


def create(name: str) -> dict:
    with _lock:
        items = _load()
        p = {"id": uuid.uuid4().hex[:8], "name": name.strip() or "新项目", "created_at": time.time()}
        items.append(p)
        _save(items)
        return p


def rename(pid: str, name: str) -> dict:
    with _lock:
        items = _load()
        p = next((x for x in items if x["id"] == pid), None)
        if not p:
            raise KeyError(pid)
        p["name"] = name.strip() or p["name"]
        _save(items)
        return p


def update(pid: str, **kw) -> dict:
    with _lock:
        items = _load()
        p = next((x for x in items if x["id"] == pid), None)
        if not p:
            raise KeyError(pid)
        p.update(kw)
        _save(items)
        return p


def pick_folder(initial: str | None = None) -> str | None:
    """在本机弹出系统“选择文件夹”对话框（服务只监听 127.0.0.1，仅本人可用）"""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(title="选择项目文件夹", initialdir=initial or None, mustexist=True)
    finally:
        root.destroy()
    return path or None


DOC_EXT = {".pdf", ".ppt", ".pptx", ".pps", ".ppsx", ".doc", ".docx", ".odp", ".odt", ".rtf",
           ".xls", ".xlsx", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"}


SKIP_DIRS = {"_work", "__pycache__", "node_modules", "$RECYCLE.BIN"}  # 工具生成的工作目录，不算课程资料


def list_files(pid: str, max_files: int = 500) -> list[dict]:
    """列出项目文件夹里的课件类文件（最多 4 层，跳过导出目录与隐藏目录）"""
    p = get(pid)
    if not p or not p.get("folder"):
        return []
    root = Path(p["folder"])
    if not root.is_dir():
        return []
    from .pipeline import is_job_dir

    out, job_dirs = [], set()
    for f in root.rglob("*"):
        rel = f.relative_to(root)
        if f.is_dir() and is_job_dir(f):
            job_dirs.add(rel.parts)  # 录音笔记自己的文件夹不算课件
        if (len(rel.parts) > 4 or any(x.startswith((".", "~$")) or x in SKIP_DIRS for x in rel.parts)
                or any(rel.parts[:len(jd)] == jd for jd in job_dirs)
                or not f.is_file() or f.suffix.lower() not in DOC_EXT):
            continue
        st = f.stat()
        out.append({"path": str(rel).replace("\\", "/"), "size": st.st_size, "mtime": st.st_mtime})
        if len(out) >= max_files:
            break
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def resolve_file(pid: str, rel: str) -> Path:
    """把相对路径解析为项目文件夹内的真实路径（防止越界访问）"""
    p = get(pid)
    if not p or not p.get("folder"):
        raise KeyError("项目未设置文件夹")
    root = Path(p["folder"]).resolve()
    f = (root / rel).resolve()
    if root not in f.parents or not f.is_file():
        raise KeyError(rel)
    return f


def delete(pid: str):
    """删除项目本身；其中的录音移到“未归类”（文件留在原处，不会删除）"""
    from . import jobs

    with _lock:
        _save([x for x in _load() if x["id"] != pid])
    for m in jobs.list_all():
        if m.get("project") == pid:
            jobs.get(m["id"]).update(project=None)
