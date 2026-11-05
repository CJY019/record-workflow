"""统一的大模型调用：Anthropic Messages / OpenAI Chat Completions / OpenAI Responses，支持图片输入

消息内容可以是字符串，或 parts 列表：
  {"type": "text", "text": "..."}
  {"type": "image", "media_type": "image/jpeg", "data": "<base64>"}
  {"type": "cache"}   # 缓存断点标记（仅 Anthropic 生效：标在前一个 part 上）
"""
import os
import threading
import time

from .config import CFG

_clients: dict = {}
_avail: dict = {"at": 0, "ids": {}}
_avail_lock = threading.Lock()


def providers() -> dict:
    return CFG["llm"]["providers"]


def resolve(model_id: str | None, default_key: str = "model") -> tuple[str, dict, str]:
    model_id = model_id or CFG["summary"][default_key]
    if ":" in model_id:
        pid, model = model_id.split(":", 1)
    else:  # 兼容旧写法：只写模型名
        pid = next((k for k, p in providers().items() if model_id in (p.get("models") or {})),
                   next(iter(providers())))
        model = model_id
    if pid not in providers():
        raise ValueError(f"未知服务商: {pid}")
    return pid, providers()[pid], model


def supports_vision(model_id: str | None, default_key: str = "model") -> bool:
    return bool(resolve(model_id, default_key)[1].get("vision"))


def _base_and_key(p: dict):
    base = p.get("base_url") or os.getenv(p.get("base_url_env", ""), "")
    base = base.rstrip("/") + p.get("base_url_suffix", "") if base else None
    return base, os.getenv(p.get("key_env", ""), "")


def _client(pid: str, p: dict):
    if pid in _clients:
        return _clients[pid]
    base, key = _base_and_key(p)
    if not key:
        raise RuntimeError(f"服务商 {pid} 缺少 key：请在 .env 或系统环境变量里设置 {p.get('key_env')}")
    timeout = CFG["summary"]["timeout_sec"]
    if p["format"] == "anthropic":
        import anthropic

        kw = {"base_url": base, "timeout": timeout, "max_retries": 2}
        kw["auth_token" if p.get("key_env", "").endswith("AUTH_TOKEN") else "api_key"] = key
        c = anthropic.Anthropic(**kw)
    else:
        import openai

        c = openai.OpenAI(base_url=base, api_key=key, timeout=timeout, max_retries=2)
    _clients[pid] = c
    return c


# ---------- 可用性检测 ----------
def _fetch_ids(pid: str, p: dict) -> set | None:
    """返回该服务商当前 key 能看到的模型 id；None 表示无法判断"""
    import httpx

    base, key = _base_and_key(p)
    if not key or not base:
        return set()
    url = base + ("/v1/models" if p["format"] == "anthropic" else "/models")
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {key}", "x-api-key": key}, timeout=6)
        if r.status_code in (401, 403):
            return set()
        r.raise_for_status()
        return {m.get("id") for m in r.json().get("data", [])}
    except Exception:
        return None


def availability(refresh: bool = False) -> dict:
    """{model_id: True/False/None}，缓存 10 分钟"""
    with _avail_lock:
        if refresh or time.time() - _avail["at"] > 600:
            ids = {}
            for pid, p in providers().items():
                seen = _fetch_ids(pid, p)
                for mid in p.get("models") or {}:
                    ids[f"{pid}:{mid}"] = None if seen is None else (mid in seen)
            _avail.update(at=time.time(), ids=ids)
        return dict(_avail["ids"])


def list_models(refresh: bool = False) -> list[dict]:
    av = availability(refresh)
    out = []
    for pid, p in providers().items():
        for mid, label in (p.get("models") or {}).items():
            mid_full = f"{pid}:{mid}"
            out.append({"id": mid_full, "label": label, "provider": p.get("label", pid),
                        "vision": bool(p.get("vision")), "available": av.get(mid_full)})
    return out


# ---------- 消息格式转换 ----------
def _parts(content):
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


def _to_anthropic(messages):
    out = []
    for m in messages:
        blocks = []
        for part in _parts(m["content"]):
            if part["type"] == "cache":
                if blocks:
                    blocks[-1]["cache_control"] = {"type": "ephemeral"}
            elif part["type"] == "text":
                blocks.append({"type": "text", "text": part["text"]})
            elif part["type"] == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": part["media_type"],
                                                           "data": part["data"]}})
        out.append({"role": m["role"], "content": blocks})
    return out


def _to_chat(messages):
    out = []
    for m in messages:
        parts = [x for x in _parts(m["content"]) if x["type"] != "cache"]
        if all(x["type"] == "text" for x in parts):
            out.append({"role": m["role"], "content": "\n\n".join(x["text"] for x in parts)})
        else:
            out.append({"role": m["role"], "content": [
                {"type": "text", "text": x["text"]} if x["type"] == "text" else
                {"type": "image_url", "image_url": {"url": f"data:{x['media_type']};base64,{x['data']}"}}
                for x in parts]})
    return out


def _to_responses(messages):
    out = []
    for m in messages:
        t_in = "output_text" if m["role"] == "assistant" else "input_text"
        out.append({"role": m["role"], "content": [
            {"type": t_in, "text": x["text"]} if x["type"] == "text" else
            {"type": "input_image", "image_url": f"data:{x['media_type']};base64,{x['data']}"}
            for x in _parts(m["content"]) if x["type"] != "cache"]})
    return out


def _strip_images(messages):
    """不支持看图的模型：去掉图片，只留文字"""
    out = []
    for m in messages:
        if isinstance(m["content"], str):
            out.append(m)
        else:
            out.append({"role": m["role"], "content": [x for x in m["content"] if x["type"] != "image"]})
    return out


def stream(system: str, messages: list[dict], model_id: str | None = None,
           cache_system: str | None = None, default_key: str = "model"):
    """流式生成文本。cache_system: 较长且多轮不变的上下文（Anthropic 下启用提示缓存）"""
    pid, p, model = resolve(model_id, default_key)
    client = _client(pid, p)
    max_tokens = p.get("max_tokens", 8000)
    if not p.get("vision"):
        messages = _strip_images(messages)
    if p["format"] == "anthropic":
        sys_blocks = [{"type": "text", "text": system}]
        if cache_system:
            sys_blocks.append({"type": "text", "text": cache_system,
                               "cache_control": {"type": "ephemeral"}})
        with client.messages.stream(model=model, max_tokens=max_tokens, system=sys_blocks,
                                    messages=_to_anthropic(messages)) as s:
            yield from s.text_stream
        return
    sys_text = system + ("\n\n" + cache_system if cache_system else "")
    if p.get("wire_api") == "responses":
        resp = client.responses.create(model=model, instructions=sys_text, input=_to_responses(messages),
                                       max_output_tokens=max_tokens, stream=True)
        for ev in resp:
            if ev.type == "response.output_text.delta":
                yield ev.delta
            elif ev.type in ("response.failed", "error"):
                raise RuntimeError(getattr(getattr(ev, "response", None), "error", None) or ev)
    else:
        resp = client.chat.completions.create(
            model=model, max_tokens=max_tokens, stream=True,
            messages=[{"role": "system", "content": sys_text}, *_to_chat(messages)])
        for chunk in resp:
            if chunk.choices and chunk.choices[0].delta.content:  # 忽略推理模型的思考内容
                yield chunk.choices[0].delta.content


def complete(system: str, user, model_id: str | None = None) -> str:
    return "".join(stream(system, [{"role": "user", "content": user}], model_id)).strip()


def label(model_id: str | None, default_key: str = "model") -> str:
    pid, p, model = resolve(model_id, default_key)
    return (p.get("models") or {}).get(model, model)
