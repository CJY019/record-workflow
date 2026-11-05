"""FastAPI 服务：uvicorn app.main:app  或  python -m app.main"""
import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import chat, jobs, llm, materials, projects, summaries
from .config import CFG
from .pipeline import STAGES
from .stages import summarize as summ

OUTPUT_FILES = {"transcript.md", "transcript.srt", "transcript.txt", "transcript.json",
                "summary.md", "asr.json", "diar.json", "segments.json"}


@asynccontextmanager
async def lifespan(_):
    jobs.start_worker()
    yield


app = FastAPI(title="录音转录总结", lifespan=lifespan)


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


@app.post("/jobs")
async def create_job(
    file: list[UploadFile] = File(...),
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
    return {"id": created[0].id, "ids": [j.id for j in created], "queue": jobs.queue_size()}


@app.get("/jobs")
def list_jobs():
    return jobs.list_all()


def _job(job_id):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    return j


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    j = _job(job_id)
    return {**j.meta, "dir": str(j.dir), "files": sorted(f for f in OUTPUT_FILES if j.f(f).exists())}


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
    j = _job(job_id)
    busy = j.meta.get("status") in ("running", "queued")
    if req.project is not None and busy:
        raise HTTPException(409, "任务正在处理，完成后才能移动")
    if req.title is not None:
        j.update(title=req.title.strip() or None)
    if req.project is not None:
        if req.project and not projects.get(req.project):
            raise HTTPException(400, "项目不存在")
        j.update(project=req.project or None)
    if not busy:  # 文件夹跟着标题/项目走（处理中则下次再移）
        try:
            j = jobs.relocate(j)
        except OSError as e:
            raise HTTPException(500, f"移动文件夹失败（可能有文件正被打开）：{e}")
    return j.meta | {"dir": str(j.dir)}


@app.post("/jobs/{job_id}/reveal")
def job_reveal(job_id: str):
    """在资源管理器中打开任务目录（仅本机）"""
    import os

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
def proj_folder(pid: str, req: FolderReq):
    import os

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
    return p | {"moved": moved, "skipped": skipped}


@app.get("/projects/{pid}/files")
def proj_files(pid: str):
    _proj(pid)
    return projects.list_files(pid)


@app.post("/projects/{pid}/reveal")
def proj_reveal(pid: str):
    import os

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
    if params.get("prompt") or params.get("model") or params.get("extra") is not None:
        params["summarize"] = True
    jobs.rerun(j, req.from_stage, params)
    return {"id": j.id, "status": "queued", "from_stage": req.from_stage}


@app.delete("/jobs/{job_id}")
def job_delete(job_id: str):
    j = _job(job_id)
    if j.meta.get("status") in ("queued", "running"):
        raise HTTPException(409, "任务正在执行")
    jobs.delete(j)
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
def info():
    return {"prompts": summ.list_prompts(), "stages": STAGES, "asr_model": CFG["asr"]["model"],
            "models": llm.list_models(), "summary_model": CFG["summary"]["model"],
            "chat_model": CFG["summary"].get("chat_model") or CFG["summary"]["model"],
            "diarize_default": CFG["diarization"]["default"],
            "quick_prompts": CFG.get("quick_prompts", [])}


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CFG["server"]["host"], port=CFG["server"]["port"])
