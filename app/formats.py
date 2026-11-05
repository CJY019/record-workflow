"""转录结果 → Markdown / SRT / 给 LLM 的纯文本"""


def ts(sec: float, srt: bool = False) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else f"{h:02d}:{m:02d}:{s:02d}"


def paragraphs(sentences: list[dict], max_chars: int = 150, gap: float = 3.0) -> list[dict]:
    """把句子合成段落：说话人变化、停顿较长或字数超限时换段"""
    paras = []
    for s in sentences:
        p = paras[-1] if paras else None
        if (p and p.get("speaker") == s.get("speaker") and s["start"] - p["end"] < gap
                and len(p["text"]) + len(s["text"]) <= max_chars):
            p["text"] += s["text"]
            p["end"] = s["end"]
        else:
            paras.append(dict(s))
    return paras


def by_part(sentences: list[dict], parts: list[dict] | None):
    """按分段（多文件合并时）分组：yield (分段名或 None, 该段的句子)"""
    if not parts or len(parts) < 2:
        yield None, sentences
        return
    for i, part in enumerate(parts):
        end = parts[i + 1]["start"] if i + 1 < len(parts) else float("inf")
        group = [s for s in sentences if part["start"] <= s["start"] < end]
        yield f"第{i + 1}部分：{part['name']}", group


def to_text(sentences: list[dict], parts: list[dict] | None = None) -> str:
    lines = []
    for header, group in by_part(sentences, parts):
        if header:
            lines.append(f"\n【{header}】")
        for p in paragraphs(group):
            spk = f"{p['speaker']}: " if p.get("speaker") else ""
            lines.append(f"[{ts(p['start'])}] {spk}{p['text']}")
    return "\n".join(lines).strip()


def to_markdown(sentences: list[dict], title: str = "转录稿",
                parts: list[dict] | None = None) -> str:
    out = [f"# {title}\n"]
    for header, group in by_part(sentences, parts):
        if header:
            out.append(f"## {header}\n")
        for p in paragraphs(group):
            spk = f" **{p['speaker']}**" if p.get("speaker") else ""
            out.append(f"`[{ts(p['start'])}]`{spk} {p['text']}\n")
    return "\n".join(out)


def to_srt(sentences: list[dict]) -> str:
    out = []
    for i, s in enumerate(sentences, 1):
        spk = f"{s['speaker']}: " if s.get("speaker") else ""
        out.append(f"{i}\n{ts(s['start'], True)} --> {ts(s['end'], True)}\n{spk}{s['text']}\n")
    return "\n".join(out)
