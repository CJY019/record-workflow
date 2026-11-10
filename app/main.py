"""FastAPI 服务：uvicorn app.main:app  或  python -m app.main"""
import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import ipaddress

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chat, jobs, library, llm, materials, projects, summaries
from .config import CFG
from .pipeline import STAGES
from .stages import summarize as summ

OUTPUT_FILES = {"transcript.md", "transcript.srt", "transcript.txt", "transcript.json",
                "summary.md", "asr.json", "diar.json", "segments.json"}


@asynccontextmanager
async def lifespan(_):
    jobs.start_worker()
    try:
        yield
    finally:
        jobs.stop_workers()


app = FastAPI(title="录音转录总结", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")

# ---- 访问控制：服务只接受本机连接；手机/平板经 VPS 中转（SSH 隧道）进来时，
#      VPS 上的 nginx 会加 X-Notes-Relay 头，据此区分「电脑本机」与「远程设备」 ----
RELAY_HEADER = "x-notes-relay"


def _ip(request: Request):
    try:
        ip = ipaddress.ip_address(request.client.host if request.client else "")
        return ip.ipv4_mapped or ip if ip.version == 6 else ip
    except ValueError:
        return None


def is_local(request: Request) -> bool:
    ip = _ip(request)
    return bool(ip and ip.is_loopback) and RELAY_HEADER not in request.headers


@app.middleware("http")
async def _access_guard(request: Request, call_next):
    ip = _ip(request)
    if not ip or not ip.is_loopback:
        return JSONResponse({"detail": "该设备无权访问"}, status_code=403)
    return await call_next(request)


def _local_only(request: Request):
    """弹出资源管理器/选择文件夹对话框的操作只会出现在电脑屏幕上，远程设备不允许调用"""
    if not is_local(request):
        raise HTTPException(403, "这个操作只能在电脑上进行")


def _opt_bool(v: str | None):
    if v is None or v == "":
        return None
    return v.lower() in ("1", "true", "yes", "on")


def _fix_name(name: str) -> str:
    """Windows 下 curl 等工具会按 GBK 发送文件名，被当成 latin-1 解码后成乱码，这里尝试还原"""
    try:
        raw = name.encode("latin-1")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return name


def _opt_int(v: str | None):
    return int(v) if v not in (None, "") else None


# ---------- 分片上传（手机经中转网络不稳：小块并行 + 失败重试） ----------
from .config import TMP_DIR  # noqa: E402

UP_DIR = TMP_DIR / "uploads"
UP_DIR.mkdir(parents=True, exist_ok=True)
for _d in UP_DIR.iterdir():  # 清理一天前没用完的分片
    if _d.is_dir() and _d.stat().st_mtime < __import__("time").time() - 86400:
        shutil.rmtree(_d, ignore_errors=True)


def _up_dir(uid: str) -> Path:
    if not uid.isalnum() or len(uid) > 40:
        raise HTTPException(400, "bad upload id")
    return UP_DIR / uid


@app.post("/uploads/{uid}/{n}")
async def upload_chunk(uid: str, n: int, request: Request):
    d = _up_dir(uid)
    d.mkdir(exist_ok=True)
    tmp = d / f"{n}.tmp"
    with open(tmp, "wb") as f:
        async for block in request.stream():
            f.write(block)
    tmp.replace(d / f"{n}")
    return {"ok": True, "size": (d / f"{n}").stat().st_size}


def _take_upload(uid: str, count: int, size: int) -> Path:
    """把分片按顺序拼成一个文件，校验总大小"""
    d = _up_dir(uid)
    parts = [d / str(i) for i in range(count)]
    if not all(p.exists() for p in parts) or sum(p.stat().st_size for p in parts) != size:
        raise HTTPException(400, "上传不完整，请重试")
    with tempfile.NamedTemporaryFile(delete=False) as out:
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)
    shutil.rmtree(d, ignore_errors=True)
    return Path(out.name)


class _Up:
    """让分片上传的文件看起来像 UploadFile（create_job 里复用同一套逻辑）"""
    def __init__(self, path: Path, name: str):
        self.filename, self.file = name, open(path, "rb")
        self.path = path


def _uploads(spec: str | None) -> list:
    out = []
    for it in json.loads(spec or "[]"):
        out.append(_Up(_take_upload(str(it["id"]), int(it["count"]), int(it["size"])), str(it["name"])))
    return out


@app.post("/jobs")
async def create_job(
    file: list[UploadFile] = File(default=[]),
    uploads: str | None = Form(None),       # JSON：[{id,count,size,name}] 分片上传好的录音
    attach_uploads: str | None = Form(None),  # 同上，课件
    merge: str | None = Form("false"),
    diarize: str | None = Form(None),
    language: str | None = Form(None),
    context: str = Form(""),
    prompt: str | None = Form(None),
    model: str | None = Form(None),
    extra: str = Form(""),
    summarize: str | None = Form("true"),
    num_speakers: str | None = Form(None),
    min_speakers: str | None = Form(None),
    max_speakers: str | None = Form(None),
    project: str | None = Form(None),
    attachments: list[UploadFile] = File(default=[]),
    project_files: str | None = Form(None),  # JSON 数组：项目文件夹里的相对路径
):
    """上传音频创建任务（file 可传多个）。merge=true 时多个文件按文件名顺序拼成一个任务，
    否则每个文件一个任务。language: Chinese/English/... 或 auto；prompt: lecture/brief/meeting"""
    if prompt and prompt not in summ.list_prompts():
        raise HTTPException(400, f"prompt 可选: {summ.list_prompts()}")
    if model:
        try:
            llm.resolve(model)
        except ValueError as e:
            raise HTTPException(400, str(e))
    saved = []
    for up in file:  # 保持上传顺序（前端已按文件名自然排序）
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            shutil.copyfileobj(up.file, tmp)
        saved.append((Path(tmp.name), _fix_name(up.filename or "audio")))
    for up in _uploads(uploads):
        up.file.close()
        saved.append((up.path, _fix_name(up.filename)))
    if not saved:
        raise HTTPException(400, "没有收到录音文件")
    attachments = [*attachments, *_uploads(attach_uploads)]
    params = {
        "diarize": _opt_bool(diarize),
        "language": "" if language == "auto" else (language or None),
        "context": context, "prompt": prompt or None, "model": model or None, "extra": extra,
        "summarize": _opt_bool(summarize) is not False,
        "num_speakers": _opt_int(num_speakers), "min_speakers": _opt_int(min_speakers),
        "max_speakers": _opt_int(max_speakers),
    }
    if project and not projects.get(project):
        raise HTTPException(400, "项目不存在")
    groups = [saved] if _opt_bool(merge) else [[s] for s in saved]
    created = []
    for g in groups:
        j = jobs.create(g, dict(params), enqueue=False, project=project or None)
        # 课件：上传的 + 从项目文件夹选的（每个任务各一份）
        for up in attachments:
            if up.filename:
                up.file.seek(0)
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    shutil.copyfileobj(up.file, tmp)
                try:
                    materials.add(j, Path(tmp.name), _fix_name(up.filename), move=True)
                except ValueError as e:
                    raise HTTPException(400, str(e))
        for rel in json.loads(project_files or "[]"):
            try:
                materials.add(j, projects.resolve_file(project, rel), Path(rel).name)
            except (KeyError, ValueError) as e:
                raise HTTPException(400, f"无法添加 {rel}: {e}")
        jobs.enqueue(j.id)
        created.append(j)
    for up in attachments:
        if isinstance(up, _Up):
            up.file.close()
            up.path.unlink(missing_ok=True)
    return {"id": created[0].id, "ids": [j.id for j in created], "queue": jobs.queue_size()}


@app.get("/jobs")
def list_jobs():
    return jobs.list_all()


@app.get("/queue")
def queue_status():
    return jobs.queue_status()


def _job(job_id):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    return j


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    j = _job(job_id)
    return {**j.meta, **jobs.queue_positions().get(job_id, {}), "dir": str(j.dir),
            "files": sorted(f for f in OUTPUT_FILES if j.f(f).exists())}


@app.get("/jobs/{job_id}/result")
def job_result(job_id: str):
    j = _job(job_id)
    out = {"id": j.id, "status": j.meta.get("status")}
    if j.f("transcript.json").exists():
        out["transcript"] = json.loads(j.f("transcript.json").read_text(encoding="utf-8"))
        out["transcript_text"] = j.f("transcript.txt").read_text(encoding="utf-8")
    if j.f("summary.md").exists():
        out["summary"] = j.f("summary.md").read_text(encoding="utf-8")
    return out


@app.get("/jobs/{job_id}/files/{name}")
def job_file(job_id: str, name: str):
    j = _job(job_id)
    if name not in OUTPUT_FILES or not j.f(name).exists():
        raise HTTPException(404, "文件不存在")
    stem = j.meta.get("title") or Path(j.meta.get("filename", "audio")).stem
    return FileResponse(j.f(name), filename=f"{stem}.{name}")


class TitleReq(BaseModel):
    title: str = ""


class JobPatch(BaseModel):
    title: str | None = None
    project: str | None = None  # "" 表示移出项目


@app.patch("/jobs/{job_id}")
def job_patch(job_id: str, req: JobPatch):
    """重命名任务 / 移动到项目"""
    try:
        j = jobs.patch_job(_job(job_id), title=req.title, project=req.project)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"移动文件夹失败（可能有文件正被打开）：{e}")
    return j.meta | {"dir": str(j.dir)}


@app.post("/jobs/{job_id}/reveal")
def job_reveal(job_id: str, request: Request):
    """在资源管理器中打开任务目录（仅本机）"""
    import os

    _local_only(request)
    j = _job(job_id)
    os.startfile(j.dir)
    return {"path": str(j.dir)}


# ---- 总结版本 ----
@app.get("/jobs/{job_id}/summaries")
def summ_list(job_id: str):
    return summaries.list_all(_job(job_id))


def _sid(fn, *a):
    try:
        return fn(*a)
    except KeyError:
        raise HTTPException(404, "总结版本不存在")


@app.get("/jobs/{job_id}/summaries/{sid}")
def summ_get(job_id: str, sid: str):
    return {"id": sid, "text": _sid(summaries.read, _job(job_id), sid)}


@app.patch("/jobs/{job_id}/summaries/{sid}")
def summ_rename(job_id: str, sid: str, req: TitleReq):
    return _sid(summaries.rename, _job(job_id), sid, req.title)


@app.post("/jobs/{job_id}/summaries/{sid}/activate")
def summ_activate(job_id: str, sid: str):
    _sid(summaries.activate, _job(job_id), sid)
    return {"active": sid}


@app.delete("/jobs/{job_id}/summaries/{sid}")
def summ_delete(job_id: str, sid: str):
    summaries.delete(_job(job_id), sid)
    return {"deleted": sid}


# ---- 项目 ----
class ProjReq(BaseModel):
    name: str


@app.get("/projects")
def proj_list():
    return projects.list_all()


@app.post("/projects")
def proj_create(req: ProjReq):
    return projects.create(req.name)


@app.patch("/projects/{pid}")
def proj_rename(pid: str, req: ProjReq):
    try:
        return projects.rename(pid, req.name)
    except KeyError:
        raise HTTPException(404, "项目不存在")


@app.delete("/projects/{pid}")
def proj_delete(pid: str):
    projects.delete(pid)
    return {"deleted": pid}


def _proj(pid):
    p = projects.get(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


class FolderReq(BaseModel):
    folder: str | None = None  # 不传则弹出系统对话框选择；"" 表示取消关联


@app.post("/projects/{pid}/folder")
def proj_folder(pid: str, req: FolderReq, request: Request):
    import os

    _local_only(request)
    p = _proj(pid)
    folder = req.folder
    if folder is None:
        folder = projects.pick_folder(p.get("folder"))
        if not folder:
            return {"cancelled": True, **p}
    if folder and not os.path.isdir(folder):
        raise HTTPException(400, "文件夹不存在")
    p = projects.update(pid, folder=os.path.normpath(folder) if folder else None)
    # 把该项目已有的录音整理进新文件夹
    moved, skipped = 0, []
    if p.get("folder"):
        for m in jobs.list_all():
            if m.get("project") == pid:
                try:
                    before = m["dir"]
                    j = jobs.relocate(jobs.get(m["id"]))
                    moved += str(j.dir) != before
                except (RuntimeError, OSError) as e:
                    skipped.append(f"{m.get('title') or m.get('filename')}: {e}")
    library.refresh_async(pid, min_interval=0)  # 换了文件夹：重建资料库
    return p | {"moved": moved, "skipped": skipped}


@app.get("/projects/{pid}/library")
def proj_library(pid: str):
    """项目资料库状态；顺便检查是否需要更新"""
    _proj(pid)
    library.refresh_async(pid)
    return library.status(pid)


@app.post("/projects/{pid}/library/rebuild")
def proj_library_rebuild(pid: str):
    _proj(pid)
    library.refresh_async(pid, min_interval=0)
    return library.status(pid) | {"running": True}


@app.get("/projects/{pid}/files")
def proj_files(pid: str):
    _proj(pid)
    return projects.list_files(pid)


@app.post("/projects/{pid}/reveal")
def proj_reveal(pid: str, request: Request):
    import os

    _local_only(request)
    p = _proj(pid)
    if not p.get("folder") or not os.path.isdir(p["folder"]):
        raise HTTPException(400, "项目还没有关联文件夹")
    os.startfile(p["folder"])
    return {"path": p["folder"]}


# ---- 课件 / 资料 ----
@app.get("/jobs/{job_id}/materials")
def mat_list(job_id: str):
    return materials.list_all(_job(job_id))


@app.post("/jobs/{job_id}/materials")
async def mat_upload(job_id: str, file: list[UploadFile] = File(...)):
    j = _job(job_id)
    out = []
    for up in file:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            shutil.copyfileobj(up.file, tmp)
        try:
            out.append(materials.add(j, Path(tmp.name), _fix_name(up.filename or "file"), move=True))
        except ValueError as e:
            raise HTTPException(400, str(e))
    return out


class PathsReq(BaseModel):
    paths: list[str]


@app.post("/jobs/{job_id}/materials/from-project")
def mat_from_project(job_id: str, req: PathsReq):
    j = _job(job_id)
    pid = j.meta.get("project")
    if not pid:
        raise HTTPException(400, "该录音不在项目里")
    out = []
    for rel in req.paths:
        try:
            out.append(materials.add(j, projects.resolve_file(pid, rel), Path(rel).name))
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"无法添加 {rel}: {e}")
    return out


@app.delete("/jobs/{job_id}/materials/{aid}")
def mat_delete(job_id: str, aid: str):
    materials.delete(_job(job_id), aid)
    return {"deleted": aid}


@app.get("/jobs/{job_id}/materials/{aid}/pages/{n}")
def mat_page(job_id: str, aid: str, n: int):
    p = materials.page_image(_job(job_id), aid, n)
    if not p:
        raise HTTPException(404, "页面不存在")
    return FileResponse(p, media_type="image/jpeg")


@app.get("/models")
def models(refresh: bool = False):
    return llm.list_models(refresh)


class RerunReq(BaseModel):
    from_stage: str = "summarize"
    prompt: str | None = None
    model: str | None = None
    extra: str | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    context: str | None = None
    language: str | None = None
    diarize: bool | None = None
    num_speakers: int | None = None


@app.post("/jobs/{job_id}/rerun")
def job_rerun(job_id: str, req: RerunReq):
    j = _job(job_id)
    if req.from_stage not in STAGES:
        raise HTTPException(400, f"from_stage 可选: {STAGES}")
    if j.meta.get("status") in ("queued", "running"):
        raise HTTPException(409, "任务正在执行")
    params = req.model_dump(exclude={"from_stage"})
    if req.from_stage == "summarize" or params.get("prompt") or params.get("model") or params.get("extra") is not None:
        params["summarize"] = True
    try:
        jobs.rerun(j, req.from_stage, params)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"id": j.id, "status": "queued", "from_stage": req.from_stage}


@app.delete("/jobs/{job_id}")
def job_delete(job_id: str):
    j = _job(job_id)
    if j.meta.get("status") in ("queued", "running"):
        raise HTTPException(409, "任务正在执行")
    try:
        jobs.delete(j)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"deleted": job_id}


class ChatReq(BaseModel):
    message: str
    model: str | None = None


class ConvReq(BaseModel):
    title: str = ""


def _conv(j, cid):
    try:
        return chat.get(j, cid)
    except KeyError:
        raise HTTPException(404, "对话不存在")


@app.get("/jobs/{job_id}/chats")
def chats_list(job_id: str):
    return chat.list_convs(_job(job_id))


@app.post("/jobs/{job_id}/chats")
def chats_create(job_id: str, req: ConvReq):
    return chat.create(_job(job_id), req.title)


@app.get("/jobs/{job_id}/chats/{cid}")
def chats_get(job_id: str, cid: str):
    return _conv(_job(job_id), cid)


@app.patch("/jobs/{job_id}/chats/{cid}")
def chats_rename(job_id: str, cid: str, req: ConvReq):
    j = _job(job_id)
    _conv(j, cid)
    return chat.rename(j, cid, req.title)


@app.delete("/jobs/{job_id}/chats/{cid}")
def chats_delete(job_id: str, cid: str):
    chat.delete(_job(job_id), cid)
    return {"deleted": cid}


@app.post("/jobs/{job_id}/chats/{cid}/messages")
def chats_send(job_id: str, cid: str, req: ChatReq):
    """发送一条提问，流式返回纯文本回答"""
    j = _job(job_id)
    if not j.f("transcript.txt").exists():
        raise HTTPException(409, "该任务还没有转录结果")
    _conv(j, cid)
    return StreamingResponse(chat.stream_answer(j, cid, req.message, req.model),
                             media_type="text/plain; charset=utf-8")


class SummReq(BaseModel):
    text: str
    prompt: str | None = None
    model: str | None = None
    extra: str = ""


@app.post("/summarize")
def summarize_text(req: SummReq):
    """直接对一段文本做总结（同步）"""
    return {"summary": summ.summarize(req.text, req.prompt, req.model, req.extra)}


@app.get("/info")
def info(request: Request):
    return {"local": is_local(request), "prompts": summ.list_prompts(), "stages": STAGES, "asr_model": CFG["asr"]["model"],
            "models": llm.list_models(), "summary_model": CFG["summary"]["model"],
            "summary_models": llm.summary_models(),
            "chat_model": CFG["summary"].get("chat_model") or CFG["summary"]["model"],
            "diarize_default": CFG["diarization"]["default"],
            "quick_prompts": CFG.get("quick_prompts", [])}


@app.get("/", response_class=HTMLResponse)
def index():
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})  # 手机/平板总能拿到最新页面


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CFG["server"]["host"], port=CFG["server"]["port"])
