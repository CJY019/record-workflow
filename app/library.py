"""项目资料库：把项目文件夹里的既往课程资料 + 同项目其他录音的转录稿/笔记建成索引，
问答和总结时按相关度检索片段交给模型参考（关键词 BM25，中文按二字切分，无需额外依赖）。

索引存放在 data/library/<项目id>.json，只重新处理有改动的文件；Office 转 PDF 的缓存在 .cache/library。
"""
import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path

from .config import CACHE_DIR, CFG, ROOT

LIB_DIR = ROOT / "data" / "library"
LIB_DIR.mkdir(parents=True, exist_ok=True)

_locks: dict[str, threading.Lock] = {}
_state: dict[str, dict] = {}          # pid -> {"running": bool, "progress": str, "checked": ts}
_mem: dict[str, tuple] = {}           # pid -> (index mtime, chunks, bm25 统计)
_glock = threading.Lock()


def _cfg() -> dict:
    return {"enabled": True, "chunk_chars": 700, "chat_chars": 12000, "summary_chars": 16000,
            "max_file_mb": 200, "max_pages": 800, **(CFG.get("library") or {})}


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _lock(pid: str) -> threading.Lock:
    with _glock:
        return _locks.setdefault(pid, threading.Lock())


def _path(pid: str) -> Path:
    return LIB_DIR / f"{pid}.json"


def _load(pid: str) -> dict:
    p = _path(pid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"sources": {}, "updated_at": 0}


def _save(pid: str, idx: dict):
    tmp = _path(pid).with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_path(pid))


# ---------- 切分 ----------
_CJK = re.compile(r"[一-鿿]+")
_WORD = re.compile(r"[a-z0-9][a-z0-9_\-\.]*[a-z0-9]|[a-z]")
_STOP = set("的了是在和与及或也就都而但这那有我你他她它们一个不要会能对把被从到中上下为以之其等".replace("", " ").split())


def terms(text: str) -> list[str]:
    t = text.lower()
    out = [w for w in _WORD.findall(t) if len(w) > 1]
    for run in _CJK.findall(t):
        if len(run) == 1:
            if run not in _STOP:
                out.append(run)
        else:
            out += [run[i:i + 2] for i in range(len(run) - 1)]
    return out


def _split(text: str, size: int) -> list[str]:
    """按段落/句子切成约 size 字的片段"""
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= size:
        return [text] if text else []
    pieces, cur = [], ""
    for para in re.split(r"(?<=[。！？!?；;\n])", text):
        if len(cur) + len(para) > size and cur:
            pieces.append(cur.strip())
            cur = ""
        while len(para) > size * 1.5:
            pieces.append(para[:size].strip())
            para = para[size:]
        cur += para
    if cur.strip():
        pieces.append(cur.strip())
    return [p for p in pieces if len(p) > 15]


# ---------- 抽取 ----------
def _pdf_pages(pdf: Path) -> list[str]:
    import fitz

    with fitz.open(str(pdf)) as doc:
        return [pg.get_text().strip() for i, pg in enumerate(doc) if i < _cfg()["max_pages"]]


def _extract_file(pid: str, f: Path) -> list[dict]:
    """返回 [{"loc": "P3", "text": ...}]"""
    from . import materials

    ext, size = f.suffix.lower(), _cfg()["chunk_chars"]
    if ext in (".txt", ".md"):
        return [{"loc": "", "text": c} for c in _split(f.read_text(encoding="utf-8", errors="replace"), size)]
    if ext == ".pdf":
        pages = _pdf_pages(f)
    elif ext in materials.OFFICE:
        out_dir = CACHE_DIR / "library" / pid / hashlib.md5(str(f).encode()).hexdigest()[:12]
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = _pdf_pages(materials._to_pdf(f, out_dir))
    else:
        return []  # 图片暂不做 OCR
    return [{"loc": f"P{n}", "text": c} for n, t in enumerate(pages, 1) for c in _split(t, size)]


def _job_sources(pid: str) -> dict:
    """同项目其他录音：转录稿 + 当前笔记"""
    from . import jobs

    out = {}
    for m in jobs.list_all():
        if m.get("project") != pid or m.get("status") != "done":
            continue
        j = jobs.get(m["id"])
        if not j:
            continue
        title = m.get("title") or Path(m.get("filename", "录音")).stem
        for kind, name in (("transcript", "transcript.txt"), ("summary", "summary.md")):
            p = j.f(name)
            if p.exists():
                st = p.stat()
                out[f"job:{m['id']}:{kind}"] = {"path": str(p), "mtime": st.st_mtime, "size": st.st_size,
                                                "name": f"{title}（{'转录稿' if kind == 'transcript' else '笔记'}）",
                                                "job": m["id"], "kind": kind}
    return out


def _file_sources(pid: str) -> dict:
    from . import projects

    p = projects.get(pid)
    if not p or not p.get("folder"):
        return {}
    root, limit = Path(p["folder"]), _cfg()["max_file_mb"] * 1024 * 1024
    out = {}
    for it in projects.list_files(pid, max_files=2000):
        f = root / it["path"]
        if it["size"] > limit or f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        out[f"file:{it['path']}"] = {"path": str(f), "mtime": it["mtime"], "size": it["size"],
                                     "name": it["path"], "kind": "file"}
    return out


def build(pid: str):
    """增量更新索引（同一项目同时只跑一个）"""
    if not enabled():
        return
    lk = _lock(pid)
    if not lk.acquire(blocking=False):
        lk.acquire()  # 已有在跑的：等它结束即可
        lk.release()
        return
    st = _state.setdefault(pid, {})
    st.update(running=True, progress="")
    try:
        idx = _load(pid)
        old = idx.get("sources", {})
        cur = {**_file_sources(pid), **_job_sources(pid)}
        new, todo = {}, []
        for key, s in cur.items():
            o = old.get(key)
            if o and o.get("mtime") == s["mtime"] and o.get("size") == s["size"]:
                new[key] = o
            else:
                todo.append((key, s))
        changed = bool(todo) or set(old) != set(cur)
        for i, (key, s) in enumerate(todo, 1):
            st["progress"] = f"{i}/{len(todo)} {s['name']}"
            entry = {k: v for k, v in s.items() if k != "path"}
            try:
                p = Path(s["path"])
                if s["kind"] == "file":
                    entry["chunks"] = _extract_file(pid, p)
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    entry["chunks"] = _chunk_job_text(text, s["kind"])
                entry["error"] = None
            except Exception as e:
                entry["chunks"], entry["error"] = [], f"{type(e).__name__}: {e}"[:300]
            new[key] = entry
            if i % 5 == 0:  # 大文件夹：边做边存，中途也能检索
                _save(pid, {"sources": {**old, **new}, "updated_at": time.time()})
        if changed:
            _save(pid, {"sources": new, "updated_at": time.time()})
    finally:
        st.update(running=False, scheduled=False, progress="", checked=time.time())
        lk.release()


def _chunk_job_text(text: str, kind: str) -> list[dict]:
    size = _cfg()["chunk_chars"]
    if kind == "transcript":  # 每块记下起始时间戳
        out, cur, loc = [], "", ""
        for ln in text.splitlines():
            if not cur:
                m = re.match(r"\[(\d[\d:]*)", ln)
                loc = m.group(1) if m else ""
            cur += ln + "\n"
            if len(cur) >= size:
                out.append({"loc": loc, "text": cur.strip()})
                cur = ""
        if cur.strip():
            out.append({"loc": loc, "text": cur.strip()})
        return out
    return [{"loc": "", "text": c} for c in _split(text, size)]


def refresh_async(pid: str, min_interval: float = 60):
    """后台检查是否需要更新（不阻塞调用方）"""
    if not pid or not enabled():
        return
    with _glock:
        st = _state.setdefault(pid, {})
        if st.get("running") or st.get("scheduled") or time.time() - st.get("checked", 0) < min_interval:
            return
        st.update(checked=time.time(), scheduled=True)
    threading.Thread(target=build, args=(pid,), daemon=True).start()


def status(pid: str) -> dict:
    idx, st = _load(pid), _state.get(pid, {})
    src = idx.get("sources", {}).values()
    return {"enabled": enabled(), "running": bool(st.get("running")), "progress": st.get("progress", ""),
            "files": sum(1 for s in src if s.get("kind") == "file"),
            "recordings": len({s.get("job") for s in src if s.get("job")}),
            "chunks": sum(len(s.get("chunks") or []) for s in src),
            "errors": [{"name": s["name"], "error": s["error"]} for s in src if s.get("error")][:20],
            "updated_at": idx.get("updated_at", 0)}


# ---------- 检索 ----------
def _stats(pid: str):
    p = _path(pid)
    mt = p.stat().st_mtime if p.exists() else 0
    cached = _mem.get(pid)
    if cached and cached[0] == mt:
        return cached[1], cached[2]
    chunks = []
    for key, s in _load(pid).get("sources", {}).items():
        for c in s.get("chunks") or []:
            chunks.append({"key": key, "name": s["name"], "job": s.get("job"), "loc": c["loc"], "text": c["text"]})
    tfs = [Counter(terms(c["name"] + " " + c["text"])) for c in chunks]
    df = Counter(t for tf in tfs for t in tf)
    n = len(chunks) or 1
    st = {"tfs": tfs, "lens": [sum(tf.values()) for tf in tfs],
          "idf": {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}}
    st["avg"] = (sum(st["lens"]) / n) or 1
    _mem[pid] = (mt, chunks, st)
    return chunks, st


def search(pid: str, query: str, budget: int, exclude_job: str | None = None,
           exclude_names: set | None = None, max_query_terms: int = 120) -> list[dict]:
    if not pid or not enabled():
        return []
    chunks, st = _stats(pid)
    if not chunks:
        return []
    q = Counter(terms(query))
    if len(q) > max_query_terms:  # 长查询（整篇转录稿）：取最有区分度的词
        q = Counter(dict(sorted(q.items(), key=lambda kv: -kv[1] * st["idf"].get(kv[0], 0))[:max_query_terms]))
    k1, b, avg = 1.2, 0.75, st["avg"]
    scored = []
    for i, tf in enumerate(st["tfs"]):
        c = chunks[i]
        if exclude_job and c["job"] == exclude_job:
            continue
        if exclude_names and Path(c["name"]).name in exclude_names:
            continue
        s = 0.0
        for t, qw in q.items():
            f = tf.get(t)
            if f:
                s += min(qw, 3) * st["idf"].get(t, 0) * f * (k1 + 1) / (f + k1 * (1 - b + b * st["lens"][i] / avg))
        if s > 0:
            scored.append((s, i))
    if not scored:
        return []
    scored.sort(reverse=True)
    top = scored[0][0]
    out, used = [], 0
    for s, i in scored:
        if s < top * 0.3 or len(out) >= 24:
            break
        c = chunks[i]
        if used + len(c["text"]) > budget:
            continue
        out.append(c | {"score": round(s, 2)})
        used += len(c["text"])
    return out


def context_text(pid: str, query: str, budget: int, exclude_job: str | None = None,
                 exclude_names: set | None = None) -> str:
    hits = search(pid, query, budget, exclude_job, exclude_names)
    if not hits:
        return ""
    body = "\n\n".join(f"[{h['name']}{' ' + h['loc'] if h['loc'] else ''}]\n{h['text']}" for h in hits)
    return ("<project_library>\n以下是从本项目资料库（既往课程资料、同课程其他录音的转录稿与笔记）中按相关度检索出的片段，"
            "可能并不都相关：只在确实有帮助时参考，引用时注明来源，如 [文件名 P页码] 或 [某讲 时间戳]；"
            "与本节课录音冲突时以本节课为准。\n\n" + body + "\n</project_library>")
