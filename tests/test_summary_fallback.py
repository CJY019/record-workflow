"""Offline integration tests for summary fallback and cancellable SDK streams.

The fake SDKs model the three wire formats without importing their real clients.
No GPU, recording job, network request, or persistent application data is used.
"""
import asyncio
import copy
import sys
import types
import unittest
from dataclasses import dataclass, field
from unittest.mock import patch

from app import llm
from app.stages import summarize


PRIMARY = "anthropic:primary-model"
BACKUP = "responses:backup-model"
TEXT_ONLY = "text-only:last-resort-model"
MANUAL = "anthropic:manual-model"
CHAIN = [PRIMARY, BACKUP, TEXT_ONLY]
HEARTBEAT = object()
ANTHROPIC_STOP = object()


def event(kind, **values):
    return types.SimpleNamespace(type=kind, **values)


def delta(text):
    return event("response.output_text.delta", delta=text)


def chat_chunk(text=None, finish=None):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        delta=types.SimpleNamespace(content=text), finish_reason=finish)])


@dataclass
class Plan:
    model: str
    steps: list = field(default_factory=list)
    stop_reason: str | None = "end_turn"
    setup_delay: float = 0


def success(model, text):
    if model.startswith("anthropic:"):
        values = [text, ANTHROPIC_STOP]
    elif model == BACKUP:
        values = [delta(text), event("response.completed")]
    else:
        values = [chat_chunk(text), chat_chunk(finish="stop")]
    return Plan(model, [(0, value) for value in values])


def failure(model, text="discard this partial draft"):
    plan = success(model, text)
    plan.steps = plan.steps[:1] + [(0, RuntimeError("simulated upstream failure"))]
    return plan


class FakeStream:
    def __init__(self, plan):
        self.plan = plan
        self.request_started = False
        self.opened = False
        self.closed = False
        self.cancelled = False
        self.setup_cancelled = False
        self.heartbeats = 0
        self.exhausted = False
        self.values_emitted = 0
        self.terminal_seen = False
        self.final_message_requested = False
        self._event_iterator = None
        self._snapshot_text = ""

    async def prepare(self):
        if self.request_started:
            return
        self.request_started = True
        try:
            await asyncio.sleep(self.plan.setup_delay)
        except asyncio.CancelledError:
            self.setup_cancelled = True
            raise

    async def __aenter__(self):
        await self.prepare()
        self.opened = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        self.closed = True
        if self._event_iterator is not None:
            await self._event_iterator.aclose()

    async def _iterate(self):
        try:
            for delay, value in self.plan.steps:
                await asyncio.sleep(delay)
                if isinstance(value, BaseException):
                    raise value
                if value is HEARTBEAT:
                    self.heartbeats += 1
                    # Anthropic filters pings before both text and event iterators.
                    if self.plan.model.startswith("anthropic:"):
                        continue
                    value = event("ping", choices=[])
                if value is ANTHROPIC_STOP:
                    self.terminal_seen = True
                    value = event("message_stop", message=types.SimpleNamespace(
                        stop_reason=self.plan.stop_reason, content=[]))
                self.values_emitted += 1
                if isinstance(value, str) and self.plan.model.startswith("anthropic:"):
                    self._snapshot_text += value
                    # The real high-level SDK emits both raw and synthesized text
                    # events; accepting both would duplicate the generated answer.
                    yield event("content_block_delta", index=0, delta=event("text_delta", text=value))
                    yield event("text", text=value, snapshot=self._snapshot_text)
                else:
                    yield value
            self.exhausted = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    @property
    def text_stream(self):
        async def texts():
            async for item in self:
                if item.type == "content_block_delta" and item.delta.type == "text_delta":
                    yield item.delta.text
        return texts()

    def __aiter__(self):
        if self._event_iterator is None:
            self._event_iterator = self._iterate()
        return self._event_iterator

    async def get_final_message(self):
        # The real SDK drains the stream here, even after a message_stop event.
        # This must not be called after breaking at a terminal event.
        self.final_message_requested = True
        async for _ in self:
            pass
        return types.SimpleNamespace(stop_reason=self.plan.stop_reason if self.terminal_seen else None)


class FakeClient:
    def __init__(self, sdk, kind, kwargs, plan):
        self.sdk = sdk
        self.kind = kind
        self.kwargs = kwargs
        self.plan = plan
        self.stream = FakeStream(plan)
        self.entered = False
        self.closed = False
        self.messages = types.SimpleNamespace(stream=self.message_stream)
        self.responses = types.SimpleNamespace(create=self.response_create)
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self.chat_create))

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True

    def record(self, protocol, kwargs):
        expected = self.plan.model.split(":", 1)[1]
        if kwargs.get("model") != expected:
            raise AssertionError(f"Expected model {expected}, got {kwargs.get('model')}")
        self.sdk.requests.append((self.plan.model, protocol, copy.deepcopy(kwargs)))

    def message_stream(self, **kwargs):
        self.record("anthropic", kwargs)
        return self.stream

    async def response_create(self, **kwargs):
        self.record("responses", kwargs)
        await self.stream.prepare()
        return self.stream

    async def chat_create(self, **kwargs):
        self.record("chat", kwargs)
        await self.stream.prepare()
        return self.stream


class FakeSDK:
    def __init__(self):
        self.plans = []
        self.clients = []
        self.requests = []
        self.anthropic = types.ModuleType("anthropic")
        self.openai = types.ModuleType("openai")
        self.anthropic.AsyncAnthropic = lambda **kw: self.make("anthropic", kw)
        self.openai.AsyncOpenAI = lambda **kw: self.make("openai", kw)

    def make(self, kind, kwargs):
        if not self.plans:
            raise AssertionError("Unexpected SDK attempt: all offline plans were consumed")
        plan = self.plans.pop(0)
        expected = "anthropic" if plan.model.startswith("anthropic:") else "openai"
        if kind != expected:
            raise AssertionError(f"Expected {expected} SDK, got {kind}")
        client = FakeClient(self, kind, kwargs, plan)
        self.clients.append(client)
        return client


class SummaryFallbackTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        providers = {
            "anthropic": {"format": "anthropic", "vision": True, "max_tokens": 16000,
                          "models": {"primary-model": "Primary Model", "manual-model": "Manual Model"}},
            "responses": {"format": "openai", "wire_api": "responses", "vision": True,
                          "max_tokens": 16000, "models": {"backup-model": "Backup Model"}},
            "text-only": {"format": "openai", "vision": False, "max_tokens": 32000,
                          "models": {"last-resort-model": "Text-only Model"}},
        }
        config = {
            "summary": {"model": PRIMARY, "fallback_models": [BACKUP, TEXT_ONLY],
                        "timeout_sec": 1, "first_token_timeout_sec": 1,
                        "idle_timeout_sec": 1, "attempt_timeout_sec": 2,
                        "default_prompt": "lecture", "single_pass_chars": 60000,
                        "chunk_chars": 20000},
            "llm": {"providers": providers},
        }
        self.enterContext(patch.dict(llm.CFG, config))
        self.enterContext(patch.dict(sys.modules, {
            "anthropic": self.sdk.anthropic, "openai": self.sdk.openai}))
        self.enterContext(patch.object(llm, "_base_and_key",
                                      return_value=("https://offline.invalid", "test-placeholder")))
        self.enterContext(patch.object(llm, "_client", side_effect=AssertionError(
            "Summary generation must not use the synchronous chat client")))
        self.enterContext(patch.object(summarize, "load_prompt", return_value="test system prompt"))

    def assert_closed(self, start=0):
        for client in self.sdk.clients[start:]:
            self.assertTrue(client.closed, "SDK client was not closed")
            self.assertTrue(client.stream.closed, "SDK stream was not closed")

    def test_default_order_and_duplicate_models(self):
        self.assertEqual(llm.summary_models(), CHAIN)
        llm.CFG["summary"]["fallback_models"] = [BACKUP, PRIMARY, BACKUP, TEXT_ONLY, TEXT_ONLY]
        self.assertEqual(llm.summary_models(), CHAIN)

    def test_manual_model_uses_only_chain_tail(self):
        for index, model in enumerate(CHAIN):
            with self.subTest(model=model):
                self.assertEqual(llm.summary_models(model), CHAIN[index:])
        self.assertEqual(llm.summary_models(MANUAL), [MANUAL, *CHAIN])

    def test_failed_drafts_are_discarded_and_attempt_order_is_reported(self):
        self.sdk.plans = [failure(PRIMARY), failure(BACKUP), success(TEXT_ONLY, "final summary")]
        attempts = []
        result = llm.complete_summary("system", "transcript", on_attempt=lambda *a: attempts.append(a))
        self.assertEqual(result, ("final summary", TEXT_ONLY))
        self.assertEqual([(a[0], a[1]) for a in attempts], [
            (PRIMARY, "calling"), (PRIMARY, "failed"), (BACKUP, "calling"),
            (BACKUP, "failed"), (TEXT_ONLY, "calling"), (TEXT_ONLY, "succeeded")])
        self.assertEqual([r[0] for r in self.sdk.requests], CHAIN)
        self.assertTrue(all(c.kwargs["max_retries"] == 0 for c in self.sdk.clients))
        self.assertIn("simulated upstream failure", attempts[1][2])
        self.assert_closed()

    def test_empty_and_whitespace_results_trigger_fallback(self):
        self.sdk.plans = [success(PRIMARY, ""), success(BACKUP, " \n\t"), success(TEXT_ONLY, "valid")]
        self.assertEqual(llm.complete_summary("system", "text"), ("valid", TEXT_ONLY))
        self.assert_closed()

    def test_all_failed_models_raise_one_error_and_close_resources(self):
        self.sdk.plans = [failure(model) for model in CHAIN]
        attempts = []
        with self.assertRaisesRegex(RuntimeError, "所有总结模型均未完成") as raised:
            llm.complete_summary("system", "text", on_attempt=lambda *a: attempts.append(a))
        self.assertIn("Primary Model", str(raised.exception))
        self.assertIn("Backup Model", str(raised.exception))
        self.assertIn("Text-only Model", str(raised.exception))
        self.assertNotIn("test-placeholder", str(raised.exception))
        self.assertNotIn("succeeded", [a[1] for a in attempts])
        self.assert_closed()

    def test_anthropic_truncated_refused_or_missing_stop_is_not_success(self):
        for reason in ("max_tokens", "refusal", None):
            with self.subTest(stop_reason=reason):
                start = len(self.sdk.clients)
                broken = success(PRIMARY, "partial opus result")
                broken.stop_reason = reason
                self.sdk.plans = [broken, success(BACKUP, "complete astra result")]
                self.assertEqual(llm.complete_summary("system", "text", [PRIMARY, BACKUP]),
                                 ("complete astra result", BACKUP))
                self.assert_closed(start)

    def test_anthropic_eof_before_message_stop_discards_partial_text(self):
        broken = success(PRIMARY, "partial without terminal event")
        broken.steps = broken.steps[:1]
        self.sdk.plans = [broken, success(BACKUP, "complete astra result")]
        self.assertEqual(llm.complete_summary("system", "text", [PRIMARY, BACKUP]),
                         ("complete astra result", BACKUP))
        self.assert_closed()

    def test_responses_failed_incomplete_and_missing_completed_are_discarded(self):
        for ending in ("response.failed", "response.incomplete", "error", None):
            with self.subTest(ending=ending):
                start = len(self.sdk.clients)
                broken = success(BACKUP, "partial astra result")
                broken.steps = broken.steps[:1]
                if ending:
                    broken.steps.append((0, event(ending)))
                self.sdk.plans = [broken, success(TEXT_ONLY, "complete deepseek result")]
                self.assertEqual(llm.complete_summary("system", "text", [BACKUP, TEXT_ONLY]),
                                 ("complete deepseek result", TEXT_ONLY))
                self.assert_closed(start)

    def test_chat_length_filter_tool_call_and_missing_stop_are_discarded(self):
        for ending in ("length", "content_filter", "tool_calls", None):
            with self.subTest(ending=ending):
                start = len(self.sdk.clients)
                broken = success(TEXT_ONLY, "partial deepseek result")
                broken.steps = broken.steps[:1]
                if ending:
                    broken.steps.append((0, chat_chunk(finish=ending)))
                self.sdk.plans = [broken, success(PRIMARY, "complete opus result")]
                self.assertEqual(llm.complete_summary("system", "text", [TEXT_ONLY, PRIMARY]),
                                 ("complete opus result", PRIMARY))
                self.assert_closed(start)

    def test_text_only_fallback_keeps_material_text_without_mutating_original(self):
        user = [{"type": "text", "text": "slide text"},
                {"type": "image", "media_type": "image/jpeg", "data": "aW1hZ2U="},
                {"type": "cache"}, {"type": "text", "text": "recording text"}]
        original = copy.deepcopy(user)
        self.sdk.plans = [failure(PRIMARY), failure(BACKUP), success(TEXT_ONLY, "summary")]
        self.assertEqual(llm.complete_summary("system", user), ("summary", TEXT_ONLY))
        anthropic_parts = self.sdk.requests[0][2]["messages"][0]["content"]
        responses_parts = self.sdk.requests[1][2]["input"][0]["content"]
        chat_content = self.sdk.requests[2][2]["messages"][1]["content"]
        self.assertTrue(any(p["type"] == "image" for p in anthropic_parts))
        self.assertTrue(any(p["type"] == "input_image" for p in responses_parts))
        self.assertEqual(chat_content, "slide text\n\nrecording text")
        self.assertEqual(user, original)
        self.assert_closed()

    def test_first_text_timeout_cancels_hidden_heartbeats_for_every_protocol(self):
        llm.CFG["summary"].update(first_token_timeout_sec=0.06, idle_timeout_sec=0.2,
                                attempt_timeout_sec=1)
        for model in CHAIN:
            with self.subTest(model=model):
                self.sdk.plans = [Plan(model, [(0.005, HEARTBEAT)] * 100)]
                with self.assertRaises(TimeoutError):
                    asyncio.run(llm._complete_summary_once("system", "text", model))
                stream = self.sdk.clients[-1].stream
                self.assertGreater(stream.heartbeats, 0)
                self.assertTrue(stream.cancelled)
                self.assertFalse(stream.exhausted)
                self.assert_closed(len(self.sdk.clients) - 1)

    def test_idle_timeout_discards_partial_text_despite_heartbeats(self):
        llm.CFG["summary"].update(first_token_timeout_sec=0.5, idle_timeout_sec=0.06,
                                attempt_timeout_sec=1)
        for model in CHAIN:
            with self.subTest(model=model):
                partial = success(model, "partial before idle")
                partial.steps = partial.steps[:1] + [(0.005, HEARTBEAT)] * 100
                self.sdk.plans = [partial]
                with self.assertRaises(TimeoutError):
                    asyncio.run(llm._complete_summary_once("system", "text", model))
                stream = self.sdk.clients[-1].stream
                self.assertTrue(stream.cancelled)
                self.assertGreater(stream.values_emitted, 0)
                self.assertFalse(stream.exhausted)
                self.assert_closed(len(self.sdk.clients) - 1)

    def test_total_deadline_cancels_even_when_body_keeps_arriving(self):
        llm.CFG["summary"].update(first_token_timeout_sec=0.5, idle_timeout_sec=0.5,
                                attempt_timeout_sec=0.08)
        for model in CHAIN:
            with self.subTest(model=model):
                value = success(model, "more text ").steps[0][1]
                self.sdk.plans = [Plan(model, [(0.005, value)] * 100)]
                with self.assertRaises(TimeoutError):
                    asyncio.run(llm._complete_summary_once("system", "text", model))
                stream = self.sdk.clients[-1].stream
                self.assertGreater(stream.values_emitted, 1)
                self.assertTrue(stream.cancelled)
                self.assertFalse(stream.exhausted)
                self.assert_closed(len(self.sdk.clients) - 1)

    def test_first_text_budget_includes_waiting_for_request_response(self):
        llm.CFG["summary"].update(first_token_timeout_sec=0.06, idle_timeout_sec=0.5,
                                attempt_timeout_sec=1)
        for model in CHAIN:
            with self.subTest(model=model):
                plan = success(model, "too late")
                plan.setup_delay = 0.5
                self.sdk.plans = [plan]
                with self.assertRaises(TimeoutError):
                    asyncio.run(llm._complete_summary_once("system", "text", model))
                client = self.sdk.clients[-1]
                self.assertTrue(client.closed)
                self.assertTrue(client.stream.setup_cancelled)
                self.assertFalse(client.stream.opened)

    def test_timeout_falls_back_after_closing_prior_stream(self):
        llm.CFG["summary"].update(first_token_timeout_sec=0.06, idle_timeout_sec=0.2,
                                attempt_timeout_sec=1)
        self.sdk.plans = [Plan(PRIMARY, [(0.005, HEARTBEAT)] * 100), success(BACKUP, "replacement")]
        attempts = []

        def report(*args):
            attempts.append(args)
            if args[:2] == (BACKUP, "calling"):
                self.assertTrue(self.sdk.clients[0].closed)
                self.assertTrue(self.sdk.clients[0].stream.closed)

        self.assertEqual(llm.complete_summary("system", "text", [PRIMARY, BACKUP], report),
                         ("replacement", BACKUP))
        self.assertIn("超时", attempts[1][2])
        self.assert_closed()

    def test_success_resets_idle_timer_from_each_text_chunk(self):
        llm.CFG["summary"].update(first_token_timeout_sec=0.12, idle_timeout_sec=0.12,
                                attempt_timeout_sec=1)
        self.sdk.plans = [Plan(PRIMARY, [(0.04, "a"), (0.04, "b"), (0.04, "c"), (0.04, "d"),
                                     (0, ANTHROPIC_STOP)])]
        self.assertEqual(llm.complete_summary("system", "text", [PRIMARY]), ("abcd", PRIMARY))
        self.assert_closed()

    def test_terminal_success_does_not_wait_for_trailing_heartbeats(self):
        llm.CFG["summary"].update(first_token_timeout_sec=0.2, idle_timeout_sec=0.06,
                                attempt_timeout_sec=0.3)
        for model in CHAIN:
            with self.subTest(model=model):
                plan = success(model, "completed answer")
                plan.steps += [(0.005, HEARTBEAT)] * 100
                self.sdk.plans = [plan]
                self.assertEqual(llm.complete_summary("system", "text", [model]),
                                 ("completed answer", model))
                stream = self.sdk.clients[-1].stream
                self.assertEqual(stream.heartbeats, 0, "Read past a terminal success event")
                self.assertFalse(stream.exhausted)
                self.assertFalse(stream.cancelled)
                self.assertFalse(stream.final_message_requested,
                                 "get_final_message would drain past message_stop and hang")
                self.assert_closed(len(self.sdk.clients) - 1)

    def test_single_pass_reports_actual_model_after_manual_selection(self):
        self.sdk.plans = [failure(BACKUP), success(TEXT_ONLY, "one pass summary")]
        actual, progress, attempts = [], [], []
        result = summarize.summarize("short recording", model=BACKUP,
            on_model=actual.append, progress=progress.append, on_attempt=lambda *a: attempts.append(a))
        self.assertEqual(result, "one pass summary")
        self.assertEqual(actual, [TEXT_ONLY])
        self.assertEqual(progress, [1.0])
        self.assertEqual([a[0] for a in attempts if a[1] == "calling"], [BACKUP, TEXT_ONLY])
        self.assert_closed()

    def test_map_reduce_promotes_fallback_and_preserves_completed_chunks(self):
        llm.CFG["summary"].update(single_pass_chars=1, chunk_chars=6)
        self.sdk.plans = [failure(PRIMARY), success(BACKUP, "first note"),
                          failure(BACKUP), success(TEXT_ONLY, "second note"),
                          success(TEXT_ONLY, "third note"), success(TEXT_ONLY, "final synthesis")]
        actual, progress, attempts = [], [], []
        result = summarize.summarize("alpha\nbravo\ncharlie\n", extra="exam focus",
            materials=[{"type": "text", "text": "course handout"}], library="related lecture",
            on_model=actual.append, progress=progress.append, on_attempt=lambda *a: attempts.append(a))
        self.assertEqual(result, "final synthesis")
        self.assertEqual(actual, [BACKUP, TEXT_ONLY, TEXT_ONLY, TEXT_ONLY])
        self.assertEqual(progress, [0.25, 0.5, 0.75, 1.0])
        self.assertEqual([a[0] for a in attempts if a[1] == "calling"],
                         [PRIMARY, BACKUP, BACKUP, TEXT_ONLY, TEXT_ONLY, TEXT_ONLY])
        request = self.sdk.requests[-1][2]
        user_content = request["messages"][1]["content"]
        for expected in ("first note", "second note", "third note", "course handout", "related lecture"):
            self.assertIn(expected, user_content)
        self.assertNotIn("discard this partial draft", user_content)
        self.assertIn("exam focus", request["messages"][0]["content"])
        self.assert_closed()

    def test_invalid_timeout_is_rejected_before_any_client_is_created(self):
        for setting in ("first_token_timeout_sec", "idle_timeout_sec", "attempt_timeout_sec"):
            with self.subTest(setting=setting), patch.dict(llm.CFG["summary"], {setting: 0}):
                with self.assertRaisesRegex(ValueError, "必须大于 0"):
                    asyncio.run(llm._complete_summary_once("system", "text", PRIMARY))
                self.assertEqual(self.sdk.clients, [])


if __name__ == "__main__":
    unittest.main()
