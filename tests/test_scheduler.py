"""Offline queue/checkpoint tests. No real models, provider calls or user jobs."""
import concurrent.futures
import contextlib
import io
import json
import queue
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app import jobs, pipeline, summaries
from app.config import CFG, TMP_DIR


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="queue-test-", dir=TMP_DIR)
        self.root = Path(self.tmp.name)
        assert TMP_DIR.resolve() in self.root.resolve().parents
        self.job_dir = self.root / "jobs"
        self.job_dir.mkdir()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.multiple(jobs, JOBS_DIR=self.job_dir, _REG=self.root / "registry.json",
            _local_q=queue.Queue(), _summary_q=queue.Queue(), _started=False, _workers=[],
            _stop=threading.Event(), _scheduler_lock=threading.RLock(), _scheduled={}))
        self.stack.enter_context(patch.object(jobs, "_hide"))
        self.stack.enter_context(patch.dict(CFG, {"pipeline": {"summary_workers": 2}}))
        self.stack.enter_context(patch("app.library.refresh_async"))
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        jobs.stop_workers(timeout=3)
        self.assertFalse(any(t.is_alive() for t in jobs._workers))
        self.stack.close()
        self.tmp.cleanup()

    def create(self, name="recording", summarize=True):
        src = self.root / f"{name}.tmp"
        src.write_bytes(b"test audio, never decoded")
        return jobs.create([(src, name + ".m4a")], {"summarize": summarize, "diarize": False}, enqueue=False)

    def until(self, check, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return
            time.sleep(.005)
        self.fail("Timed out waiting for worker state")

    def checkpoints(self, job):
        with wave.open(str(job.f("audio.wav")), "wb") as f:
            f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            f.writeframes(b"\0" * 320)
        pipeline._wj(job.f("segments.json"), [{"start": 0, "end": .01}])
        pipeline._wj(job.f("asr.json"), {"language": "Chinese", "sentences": []})
        pipeline._wj(job.f("transcript.json"), {"sentences": []})
        pipeline._write_text(job.f("transcript.txt"), "ready transcript")

    def test_summary_wait_does_not_block_next_asr_and_summary_concurrency_is_bounded(self):
        records = [self.create(name) for name in ("A", "B", "C")]
        entered, local_counts, summary_counts = [], [0, 0], [0, 0]
        lock = threading.Lock()

        def local(job, stage):
            with lock:
                local_counts[0] += 1
                local_counts[1] = max(local_counts)
                entered.append(job.meta["filename"])
            job.update(status="running", stage="asr")
            time.sleep(.015)
            with lock:
                local_counts[0] -= 1
            return True

        def summary(job):
            job.update(status="running", stage="summarize")
            with lock:
                summary_counts[0] += 1
                summary_counts[1] = max(summary_counts)
            self.release.wait(3)
            job.update(status="done")
            with lock:
                summary_counts[0] -= 1

        self.stack.enter_context(patch.object(jobs, "run_local", local))
        self.stack.enter_context(patch.object(jobs, "run_summary", summary))
        for job in records:
            self.assertTrue(jobs.enqueue(job.id))
        self.assertFalse(jobs.enqueue(records[0].id))
        jobs.start_worker(recover=False)
        self.until(lambda: len(entered) == 3 and jobs.queue_status()["summary"] == {"queued": 1, "running": 2})
        self.assertFalse(self.release.is_set())
        self.assertEqual(entered, ["A.m4a", "B.m4a", "C.m4a"])
        self.assertEqual(local_counts[1], 1)
        self.assertEqual(summary_counts[1], 2)
        self.assertEqual(jobs.queue_positions()[records[2].id]["queue_position"], 1)
        self.release.set()
        self.until(lambda: not jobs._scheduled)

    def test_summary_rerun_bypasses_local_queue_and_duplicate_reruns_are_atomic(self):
        job = self.create()
        job.update(status="done")
        barrier = threading.Barrier(2)

        def rerun():
            barrier.wait()
            try:
                jobs.rerun(job, "summarize", {})
                return True
            except RuntimeError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(rerun) for _ in range(2)]
            self.assertEqual(sorted(f.result() for f in futures), [False, True])
        self.assertEqual(jobs.queue_status()["summary"]["queued"], 1)
        self.assertEqual(jobs.queue_status()["local"]["queued"], 0)
        with self.assertRaises(RuntimeError):
            jobs.delete(job)
        with self.assertRaises(RuntimeError):
            jobs.relocate(job)
        with self.assertRaises(RuntimeError):
            jobs.patch_job(job, project="different-project")
        self.assertIsNone(jobs.get(job.id).meta.get("project"))
        self.assertTrue(job.dir.exists())

    def test_worker_survives_an_unreadable_job(self):
        a, b = self.create("bad"), self.create("good")
        jobs.enqueue(a.id)
        jobs.enqueue(b.id)
        original = jobs.get

        def get(jid):
            if jid == a.id and threading.current_thread().name.startswith("notes-"):
                raise ValueError("simulated corrupt metadata")
            return original(jid)

        self.stack.enter_context(patch.object(jobs, "get", get))
        self.stack.enter_context(patch.object(jobs, "run_local", lambda job, stage: job.update(status="done") or False))
        with contextlib.redirect_stderr(io.StringIO()):
            jobs.start_worker(recover=False)
            self.until(lambda: original(b.id).meta["status"] == "done")
        self.assertTrue(jobs._workers[0].is_alive())

    def test_resume_uses_committed_checkpoint_instead_of_old_outputs(self):
        job = self.create()
        self.checkpoints(job)
        job.update(resume_from="preprocess", stage="asr")
        self.assertEqual(jobs._resume_stage(job), "preprocess")
        job.update(resume_from="asr")
        self.assertEqual(jobs._resume_stage(job), "asr")
        job.update(resume_from="diarize")
        self.assertEqual(jobs._resume_stage(job), "diarize")
        job.f("asr.json").unlink()
        self.assertEqual(jobs._resume_stage(job), "asr")
        job.update(resume_from="summarize")
        self.assertEqual(jobs._resume_stage(job), "summarize")
        job.f("transcript.txt").unlink()
        self.assertEqual(jobs._resume_stage(job), "asr")

    def test_restart_recovers_old_summary_and_local_queue_but_not_uncommitted_upload(self):
        a, b, incomplete = self.create("A"), self.create("B"), self.create("incomplete")
        self.checkpoints(a)
        a.update(status="running", stage="summarize", dispatch_ready=True)
        b.update(status="queued", stage=None, dispatch_ready=True)
        seen = []

        def local(job, stage):
            seen.append((job.id, stage))
            job.update(status="done")
            return False

        def summary(job):
            seen.append((job.id, "summary"))
            job.update(status="done")

        self.stack.enter_context(patch.object(jobs, "run_local", local))
        self.stack.enter_context(patch.object(jobs, "run_summary", summary))
        jobs.start_worker()
        self.until(lambda: len(seen) == 2)
        self.assertCountEqual(seen, [(a.id, "summary"), (b.id, "preprocess")])
        self.assertEqual(jobs.get(incomplete.id).meta["status"], "error")

    def test_stale_job_updates_preserve_concurrent_title_and_model_fields(self):
        first = self.create()
        stale = pipeline.Job(first.dir)
        first.update(title="edited title", summary_model="actual-backup")
        stale.update(progress=.75, stage="summarize")
        meta = pipeline.Job(first.dir).meta
        self.assertEqual(meta["title"], "edited title")
        self.assertEqual(meta["summary_model"], "actual-backup")
        self.assertEqual(meta["progress"], .75)
        self.assertFalse(list(first.meta_path.parent.glob("*.tmp")))

    def test_local_no_summary_exports_transcript_and_finishes_without_api(self):
        import numpy as np
        from app.stages import asr, diarize, preprocess, vad

        job = self.create(summarize=False)
        values = {"language": "Chinese", "sentences": [{"start": 0., "end": .01, "text": "example"}]}
        with patch.object(preprocess, "concat_to_wav16k", return_value=[]), \
             patch.object(preprocess, "load_wav", return_value=np.zeros(160, dtype=np.float32)), \
             patch.object(vad, "segment", return_value=[{"start": 0, "end": .01}]), \
             patch.object(asr, "transcribe", return_value=values), \
             patch.object(diarize, "diarize", side_effect=AssertionError("unexpected speaker model")):
            self.assertFalse(pipeline.run_local(job))
        self.assertEqual(job.meta["status"], "done")
        for name in ("transcript.json", "transcript.txt", "transcript.md", "transcript.srt"):
            self.assertTrue(job.f(name).exists(), name)

    def test_summary_rerun_never_loads_audio_records_actual_model_and_is_idempotent(self):
        from app import llm, materials
        from app.stages import preprocess, summarize

        job = self.create()
        self.checkpoints(job)
        job.update(run_id="same-run")
        actual = llm.summary_models()[-1]

        def generate(*args, **kw):
            kw["on_attempt"](actual, "calling", "")
            kw["on_attempt"](actual, "succeeded", "")
            kw["on_model"](actual)
            return "completed notes"

        with patch.object(preprocess, "load_wav", side_effect=AssertionError("summary loaded audio")), \
             patch.object(materials, "wait_ready"), patch.object(materials, "context_parts", return_value=[]), \
             patch.object(pipeline, "_library_context", return_value=""), \
             patch.object(summarize, "summarize", side_effect=generate) as call:
            pipeline.run_summary(job)
            pipeline.run_summary(job)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(job.meta["status"], "done")
        self.assertEqual(job.meta["summary_model"], actual)
        versions = summaries.list_all(job)["items"]
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["model"], actual)
        self.assertEqual(versions[0]["run_id"], "same-run")

    def test_cli_wrapper_keeps_synchronous_flow(self):
        job = self.create()
        with patch.object(pipeline, "run_local", return_value=True) as local, \
             patch.object(pipeline, "run_summary") as summary:
            pipeline.run(job, "asr")
            local.assert_called_once_with(job, "asr")
            summary.assert_called_once_with(job)
        first_run = job.meta["run_id"]
        with patch.object(pipeline, "run_local", side_effect=AssertionError("summary entered local lane")), \
             patch.object(pipeline, "run_summary") as summary:
            pipeline.run(job, "summarize")
            summary.assert_called_once_with(job)
        self.assertNotEqual(job.meta["run_id"], first_run)
        self.assertTrue(job.meta["dispatch_ready"])

    def test_summary_guard_respects_no_summary_even_with_legacy_checkpoint(self):
        from app.stages import summarize

        job = self.create(summarize=False)
        job.update(resume_from="summarize")
        with patch.object(summarize, "summarize", side_effect=AssertionError("unexpected paid summary")):
            pipeline.run_summary(job)
        self.assertEqual(job.meta["status"], "done")

    def test_one_corrupt_job_does_not_break_registry_or_worker_start(self):
        good = self.create("good")
        good.update(status="done")
        broken = self.job_dir / "broken" / ".work"
        broken.mkdir(parents=True)
        (broken / "meta.json").write_text("{broken", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(jobs.get(good.id).id, good.id)
            self.assertEqual(len(jobs.list_all()), 1)
            jobs.start_worker()
        self.assertEqual(len(jobs._workers), 3)


if __name__ == "__main__":
    unittest.main()
