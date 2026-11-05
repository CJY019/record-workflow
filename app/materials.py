"""课件/资料：每个任务的 attachments/ 目录
- Office 文档（ppt/pptx/doc/docx/odp/odt/rtf/xls/xlsx）用 LibreOffice 无界面转 PDF
- PDF 用 PyMuPDF 抽每页文字 + 渲染页面图片（给能看图的模型“看见”课件）
- 图片直接作为一页；txt/md 只取文字
"""
import base64
import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import CACHE_DIR, CFG
from .pipeline import Job

OFFICE = {".ppt", ".pptx", ".pps", ".ppsx", ".doc", ".docx", ".odp", ".odt", ".rtf", ".xls", ".xlsx"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
TEXT = {".txt", ".md"}
ALLOWED = OFFICE | IMAGES | TEXT | {".pdf"}

_lock = threading.Lock()
_conv_lock = threading.Lock()  # LibreOffice 同时只跑一个


def _cfg():
    return CFG.get("materials", {})


def _dir(job: Job) -> Path:
    d = job.f("attachments")
    d.mkdir(exist_ok=True)
    return d


def _work(job: Job, aid: str) -> Path:
    d = job.f("materials_work") / aid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index(job: Job) -> list[dict]:
    p = job.f("materials.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _write(job: Job, items: list[dict]):
    job.f("materials.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _update(job: Job, aid: str, **kw):
    with _lock:
        items = _index(job)
        for it in items:
            if it["id"] == aid:
                it.update(kw)
        _write(job, items)


def list_all(job: Job) -> list[dict]:
    return [{k: v for k, v in it.items() if k != "pages"} | {"page_count": len(it.get("pages") or [])}
            for it in _index(job)]


def pending(job: Job) -> bool:
    return any(it["status"] == "processing" for it in _index(job))


def add(job: Job, src: Path, name: str, move: bool = False) -> dict:
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError(f"不支持的文件类型 {ext}，支持：{' '.join(sorted(ALLOWED))}")
    aid = uuid.uuid4().hex[:8]
    base = Path(name).name
    dst, i = _dir(job) / base, 2
    while dst.exists():
        dst, i = _dir(job) / f"{Path(base).stem} ({i}){Path(base).suffix}", i + 1
    (shutil.move if move else shutil.copy)(str(src), dst)
    item = {"id": aid, "name": dst.name, "file": dst.name, "status": "processing",
            "added_at": time.time(), "size": dst.stat().st_size}
    with _lock:
        items = _index(job)
        items.append(item)
        _write(job, items)
    threading.Thread(target=_process, args=(job, aid), daemon=True).start()
    return item


def delete(job: Job, aid: str):
    with _lock:
        items = _index(job)
        for it in items:
            if it["id"] == aid:
                (_dir(job) / it["file"]).unlink(missing_ok=True)
        _write(job, [it for it in items if it["id"] != aid])
    if aid.isalnum():
        shutil.rmtree(job.f("materials_work") / aid, ignore_errors=True)


def page_image(job: Job, aid: str, n: int) -> Path | None:
    if not aid.isalnum():
        return None
    p = job.f("materials_work") / aid / "pages" / f"{n:03d}.jpg"
    return p if p.exists() else None


def _soffice() -> str:
    for c in [_cfg().get("soffice"), shutil.which("soffice"), r"C:\Program Files\LibreOffice\program\soffice.exe"]:
        if c and Path(c).exists():
            return c
    raise RuntimeError("找不到 LibreOffice（soffice），无法转换 Office 文档；可在 config.yaml 的 materials.soffice 指定路径")


def _to_pdf(src: Path, out_dir: Path) -> Path:
    profile = (CACHE_DIR / "libreoffice").as_uri()  # 配置目录放工作区，不写 C 盘
    with _conv_lock:
        r = subprocess.run([_soffice(), f"-env:UserInstallation={profile}", "--headless", "--norestore",
                            "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
                           capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
    pdf = out_dir / (src.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(f"转换 PDF 失败：{(r.stderr or r.stdout).strip()[:300]}")
    return pdf


def _process(job: Job, aid: str):
    import fitz

    try:
        it = next(x for x in _index(job) if x["id"] == aid)
        d = _work(job, aid)
        src = _dir(job) / it["file"]
        ext = src.suffix.lower()
        pages_dir = d / "pages"
        pages_dir.mkdir(exist_ok=True)
        long_side = _cfg().get("image_long_side", 1280)
        pages = []
        if ext in TEXT:
            pages = [{"n": 1, "text": src.read_text(encoding="utf-8", errors="replace"), "img": False}]
        elif ext in IMAGES:
            pg = fitz.open(str(src))[0]
            zoom = min(1.0, long_side / max(pg.rect.width, pg.rect.height))
            pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).save(str(pages_dir / "001.jpg"), jpg_quality=75)
            pages = [{"n": 1, "text": "", "img": True}]
        else:
            pdf = src if ext == ".pdf" else _to_pdf(src, d)
            doc = fitz.open(str(pdf))
            for i, pg in enumerate(doc, 1):
                zoom = long_side / max(pg.rect.width, pg.rect.height)
                pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).save(str(pages_dir / f"{i:03d}.jpg"), jpg_quality=75)
                pages.append({"n": i, "text": pg.get_text().strip(), "img": True})
        _update(job, aid, status="ready", pages=pages, error=None)
    except Exception as e:
        _update(job, aid, status="error", error=f"{type(e).__name__}: {e}")


def wait_ready(job: Job, timeout: float = 600):
    t0 = time.time()
    while pending(job) and time.time() - t0 < timeout:
        time.sleep(1)


# ---------- 给模型的上下文 ----------
def has_materials(job: Job) -> bool:
    return any(it["status"] == "ready" for it in _index(job))


def context_parts(job: Job, vision: bool) -> list[dict]:
    """课件内容 → 消息 parts：文字（按页）+ 可选的页面图片"""
    items = [it for it in _index(job) if it["status"] == "ready"]
    if not items:
        return []
    c = _cfg()
    max_chars, max_images = c.get("max_text_chars", 80000), c.get("max_images", 60)
    total_pages = sum(len(it["pages"]) for it in items)
    # 图片过多时均匀抽样
    step = max(1, -(-total_pages // max_images)) if vision else 0
    parts = [{"type": "text", "text": "以下是本节课的课件/资料。页码记作 [文件名 P页码]，请在总结或回答中引用对应页码。"
                                       + ("课件页面图片紧跟在每页文字之后。" if vision else "")}]
    used, k, imgs = 0, 0, 0
    for it in items:
        parts.append({"type": "text", "text": f"\n===== 文件：{it['name']}（共 {len(it['pages'])} 页）====="})
        for pg in it["pages"]:
            text = pg["text"][:4000]
            if used < max_chars:
                parts.append({"type": "text", "text": f"[{it['name']} P{pg['n']}]\n{text}" if text else
                              f"[{it['name']} P{pg['n']}]（无可提取文字）"})
                used += len(text)
            if vision and pg.get("img") and k % step == 0 and imgs < max_images:
                img = job.f("materials_work") / it["id"] / "pages" / f"{pg['n']:03d}.jpg"
                if img.exists():
                    parts.append({"type": "image", "media_type": "image/jpeg",
                                  "data": base64.b64encode(img.read_bytes()).decode()})
                    imgs += 1
            k += 1
    return parts


def summary_text(job: Job) -> str:
    """纯文字版（用于不支持多模态的地方）"""
    return "\n".join(p["text"] for p in context_parts(job, vision=False) if p["type"] == "text")
