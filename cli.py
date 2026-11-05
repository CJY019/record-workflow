"""命令行直接跑：python cli.py 录音.mp3 [更多文件...] [--merge] [--diarize] [--prompt brief] [--context "课程名"]"""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from app import jobs  # noqa: E402
from app.config import JOBS_DIR  # noqa: E402
from app.pipeline import STAGES, Job, run  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="*", help="音频/视频文件，可多个")
    ap.add_argument("--merge", action="store_true", help="多个文件按文件名顺序合并为一个任务")
    ap.add_argument("--diarize", action=argparse.BooleanOptionalAction, default=None)
    ap.add_argument("--language", default=None, help="Chinese / English / auto")
    ap.add_argument("--context", default="")
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--extra", default="")
    ap.add_argument("--num-speakers", type=int, default=None)
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--rerun", metavar="JOB_ID", help="对已有任务重跑")
    ap.add_argument("--from-stage", default="summarize", choices=STAGES)
    ap.add_argument("-o", "--out", default=None, help="把 transcript.md / summary.md 复制到此目录")
    a = ap.parse_args()

    params = {"diarize": a.diarize, "language": "" if a.language == "auto" else a.language,
              "context": a.context, "prompt": a.prompt, "model": a.model, "extra": a.extra,
              "summarize": not a.no_summary, "num_speakers": a.num_speakers}

    if a.rerun:
        job = jobs.get(a.rerun)
        if not job:
            ap.error(f"找不到任务 {a.rerun}")
        job.meta["params"].update({k: v for k, v in params.items() if v not in (None, "")})
        job.save()
        from_stage = a.from_stage
    else:
        if not a.audio:
            ap.error("需要音频文件")
        files = []
        for f in sorted(a.audio):
            src = Path(f)
            tmp = Path(tempfile.mkdtemp()) / src.name
            shutil.copy(src, tmp)
            files.append((tmp, src.name))
        groups = [files] if a.merge else [[x] for x in files]
        for g in groups:
            job = jobs.create(g, dict(params), enqueue=False)  # 同步执行，不入队
            process(job, "preprocess", a.out)
        return
    process(job, from_stage, a.out)


def process(job, from_stage, out_dir):
    print(f"任务 {job.id}  目录 {job.dir}")
    run(job, from_stage)
    job = Job(job.dir)
    if job.meta["status"] != "done":
        print("失败:", job.meta.get("error"))
        return
    print(f"完成，耗时 {job.meta.get('elapsed_sec')}s")
    for name in ("transcript.md", "summary.md"):
        if job.f(name).exists():
            print("  ", job.f(name))
            if out_dir:
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                shutil.copy(job.f(name), Path(out_dir) / f"{Path(job.meta['filename']).stem}.{name}")


if __name__ == "__main__":
    main()
