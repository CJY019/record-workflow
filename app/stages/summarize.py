"""⑤ 调用大模型（见 config.yaml 的 llm.providers）生成总结；长文本 map-reduce"""
from ..config import CFG, PROMPTS_DIR


def list_prompts() -> list[str]:
    return sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))


def load_prompt(name: str) -> str:
    p = PROMPTS_DIR / f"{name}.md"
    if not p.exists():
        raise ValueError(f"prompt 不存在: {name}，可选: {list_prompts()}")
    return p.read_text(encoding="utf-8")


def chat(system: str, user, model: str | None = None) -> str:
    from .. import llm

    return llm.complete(system, user, model)


def _chunks(text: str, size: int) -> list[str]:
    lines, out, cur = text.splitlines(keepends=True), [], ""
    for ln in lines:
        if len(cur) + len(ln) > size and cur:
            out.append(cur)
            cur = ""
        cur += ln
    if cur:
        out.append(cur)
    return out


MATERIALS_HINT = """
# 课件
用户同时提供了本节课的课件/资料（文字，以及能看图时的页面图片）。请：
- 以老师的讲授为主线，把讲到的内容与课件对应起来，在大纲和知识点后标注课件页码，如 [第2讲.pptx P12]；
- 用课件纠正转录中的术语、公式、人名识别错误；课件上的公式、图表要结合讲解说明含义；
- 课件里有但老师没讲或一带而过的重要内容，单独列在「课件补充」小节；
- 老师讲了但课件上没有的内容（例子、口头强调、考试提示）要特别保留。"""


LIBRARY_HINT = """
# 项目资料库
用户还提供了从本课程既往资料、其他几讲录音中检索到的相关片段（<project_library>）。请：
- 以本节课录音为主，资料库只作背景：用来补充前后课的联系（如「第1讲讲过的××」）、纠正术语、对应教材章节；
- 引用时注明来源，如 [文件名 P页码]；不相关的片段直接忽略，不要为了引用而引用。"""


def summarize(transcript: str, prompt: str | None = None, model: str | None = None,
              extra: str = "", progress=None, materials: list | None = None, library: str = "",
              on_attempt=None, on_model=None) -> str:
    from .. import llm

    c = CFG["summary"]
    chain = llm.summary_models(model)
    current_model = 0

    def generate(system, user):
        nonlocal current_model
        text, actual = llm.complete_summary(system, user, chain[current_model:], on_attempt)
        current_model = chain.index(actual)
        if on_model:
            on_model(actual)
        return text
    system = load_prompt(prompt or c["default_prompt"])
    if library:
        system += LIBRARY_HINT
        materials = [*(materials or []), {"type": "text", "text": library}]
    if materials:
        system += MATERIALS_HINT
    if extra:
        system += f"\n\n# 补充要求\n{extra}"

    def with_materials(text: str):
        return [*materials, {"type": "text", "text": text}] if materials else text

    if len(transcript) <= c["single_pass_chars"]:
        out = generate(system, with_materials(f"以下是录音转录稿：\n\n<transcript>\n{transcript}\n</transcript>"))
        if progress:
            progress(1.0)
        return out

    parts = _chunks(transcript, c["chunk_chars"])
    notes = []
    for i, part in enumerate(parts, 1):
        notes.append(generate(
            "你是笔记助手。请对这段录音转录片段做详细笔记：按顺序列出讲到的所有要点、定义、例子、"
            "数据和结论，保留时间戳，不要遗漏细节，不要写开场白。",
            f"第 {i}/{len(parts)} 段：\n\n<transcript>\n{part}\n</transcript>"))
        if progress:
            progress(i / (len(parts) + 1))
    merged = "\n\n".join(f"## 片段 {i}\n{n}" for i, n in enumerate(notes, 1))
    out = generate(system, with_materials("录音较长，已分段整理成笔记，请基于这些笔记完成整体总结：\n\n"
                                      f"<notes>\n{merged}\n</notes>"))
    if progress:
        progress(1.0)
    return out
