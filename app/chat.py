"""录音问答：每个任务下可有多个对话，存放在 <job>/chats/<cid>.json"""
import json
import threading
import time
import uuid

from . import llm
from .config import CFG
from .pipeline import Job

SYSTEM = """你是用户的学习助手。下面是一段录音（通常是课堂/讲座）的自动转录稿和整理好的总结。
请基于这些内容回答用户的问题：
- 优先依据转录稿原文，引用时标注时间戳，如 [00:12:30]。
- 转录稿可能有同音字等识别错误，可结合上下文纠正。
- 录音里没讲到的内容，可以用你自己的知识补充，但要明确说明"录音中未提及"。
- 用中文回答，简洁清楚，公式用 LaTeX（行内 $...$，独立 $$...$$）。"""

_lock = threading.Lock()


def _dir(job: Job):
    d = job.f("chats")
    d.mkdir(exist_ok=True)
    old = job.f("chat.json")  # 迁移旧版单一聊天记录
    if old.exists():
        msgs = json.loads(old.read_text(encoding="utf-8"))
        if msgs:
            t = old.stat().st_mtime
            _save(job, {"id": uuid.uuid4().hex[:10], "title": _fallback_title(msgs[0]["content"]),
                        "created_at": t, "updated_at": t, "messages": msgs}, d)
        old.unlink()
    return d


def _path(job: Job, cid: str):
    if not cid.isalnum():
        raise KeyError(cid)
    return _dir(job) / f"{cid}.json"


BAD = '<>:"/\\|?*'


def _save(job: Job, conv: dict, d=None):
    _write_md(job, conv)
    p = (d or _dir(job)) / f"{conv['id']}.json"
    p.write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_md(job: Job, conv: dict):
    """问答记录/<标题>.md：给人看的副本，标题改了就改名"""
    md_dir = job.f("chats_md")
    md_dir.mkdir(parents=True, exist_ok=True)
    old = conv.get("md_file")
    if not conv.get("messages"):
        if old:
            (md_dir / old).unlink(missing_ok=True)
            conv["md_file"] = None
        return
    base = "".join("_" if c in BAD else c for c in conv["title"]).strip(" .")[:60] or "对话"
    name, i = f"{base}.md", 2
    while (md_dir / name).exists() and name != old:
        name, i = f"{base} ({i}).md", i + 1
    if old and old != name:
        (md_dir / old).unlink(missing_ok=True)
    lines = [f"# {conv['title']}\n"]
    for m in conv["messages"]:
        lines.append(("## 问\n\n" if m["role"] == "user" else "## 答\n\n") + m["content"] + "\n")
    (md_dir / name).write_text("\n".join(lines), encoding="utf-8")
    conv["md_file"] = name


def _fallback_title(q: str) -> str:
    q = " ".join(q.split())
    return q[:20] + ("…" if len(q) > 20 else "")


def list_convs(job: Job) -> list[dict]:
    out = []
    for p in _dir(job).glob("*.json"):
        c = json.loads(p.read_text(encoding="utf-8"))
        out.append({k: c.get(k) for k in ("id", "title", "created_at", "updated_at")}
                   | {"count": len(c.get("messages", []))})
    return sorted(out, key=lambda c: c["updated_at"] or 0, reverse=True)


def get(job: Job, cid: str) -> dict:
    p = _path(job, cid)
    if not p.exists():
        raise KeyError(cid)
    return json.loads(p.read_text(encoding="utf-8"))


def create(job: Job, title: str = "") -> dict:
    now = time.time()
    conv = {"id": uuid.uuid4().hex[:10], "title": title or "新对话", "auto_title": not title,
            "created_at": now, "updated_at": now, "messages": []}
    _save(job, conv)
    return conv


def rename(job: Job, cid: str, title: str) -> dict:
    with _lock:
        conv = get(job, cid)
        conv["title"] = title.strip() or conv["title"]
        conv["auto_title"] = False
        _save(job, conv)
    return conv


def delete(job: Job, cid: str):
    try:
        conv = get(job, cid)
        if conv.get("md_file"):
            (job.f("chats_md") / conv["md_file"]).unlink(missing_ok=True)
    except KeyError:
        pass
    _path(job, cid).unlink(missing_ok=True)


def _context(job: Job) -> str:
    parts = [f"录音标题：{job.meta.get('filename', '')}"]
    if job.f("summary.md").exists():
        parts.append("<summary>\n" + job.f("summary.md").read_text(encoding="utf-8") + "\n</summary>")
    parts.append("<transcript>\n" + job.f("transcript.txt").read_text(encoding="utf-8") + "\n</transcript>")
    return "\n\n".join(parts)


def _auto_title(job: Job, cid: str, question: str, answer: str):
    """后台用小模型起标题；失败则保留截断的问题"""
    try:
        t = llm.complete("为这段对话起一个简短的中文标题（不超过 12 个字），只输出标题本身，不要引号和标点。",
                         f"问：{question}\n答：{answer[:1500]}",
                         CFG["summary"].get("title_model")).strip().strip("\"'“”《》。")
        if t:
            with _lock:
                conv = get(job, cid)
                if conv.get("auto_title"):
                    conv["title"] = t[:30]
                    _save(job, conv)
    except Exception:
        pass


def stream_answer(job: Job, cid: str, question: str, model: str | None = None):
    """生成器：逐段 yield 回答文本；结束后写入该对话"""
    conv = get(job, cid)
    first = not conv["messages"]
    api_msgs = [{"role": m["role"], "content": m["content"]} for m in conv["messages"][-20:]]
    from . import library, materials

    lib = ""
    pid = job.meta.get("project")
    if pid and library.enabled():
        library.refresh_async(pid)
        prev = next((m["content"] for m in reversed(conv["messages"]) if m["role"] == "user"), "")
        try:
            lib = library.context_text(pid, question + "\n" + prev, library._cfg()["chat_chars"],
                                       exclude_job=job.id,
                                       exclude_names={m["name"] for m in materials.list_all(job)})
        except Exception:
            lib = ""
    api_msgs.append({"role": "user", "content": f"{lib}\n\n{question}" if lib else question})

    mats = materials.context_parts(job, llm.supports_vision(model, "chat_model"))
    if mats:  # 课件作为对话开头的固定上下文（Anthropic 下会被缓存）
        api_msgs = [{"role": "user", "content": [*mats, {"type": "cache"}]},
                    {"role": "assistant", "content": "好的，我已看过课件，会结合课件和录音回答。"}, *api_msgs]
    answer = ""
    try:
        for text in llm.stream(SYSTEM, api_msgs, model, cache_system=_context(job),
                               default_key="chat_model"):
            answer += text
            yield text
    except Exception as e:  # 出错也把已生成部分和错误信息返回给前端
        err = f"\n\n[出错：{type(e).__name__}: {e}]"
        answer += err
        yield err
    with _lock:
        conv = get(job, cid)
        now = time.time()
        conv["messages"] += [{"role": "user", "content": question, "at": now},
                             {"role": "assistant", "content": answer, "model": model or "", "at": now}]
        conv["updated_at"] = now
        if first and conv.get("auto_title"):
            conv["title"] = _fallback_title(question)
        _save(job, conv)
    if first and conv.get("auto_title"):
        threading.Thread(target=_auto_title, args=(job, cid, question, answer), daemon=True).start()
