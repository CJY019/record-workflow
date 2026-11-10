"""CC Switch credential updates stay scoped to their provider and invalidate cached clients."""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import llm
from app.config import CFG, TMP_DIR


class ProviderCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="credentials-test-", dir=TMP_DIR)
        self.root = Path(self.tmp.name)
        assert TMP_DIR.resolve() in self.root.resolve().parents
        (self.root / ".claude").mkdir()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(llm.Path, "home", return_value=self.root))
        self.stack.enter_context(patch.dict(os.environ, {"RECORD_TEST_AUTH_TOKEN": "saved-provider-key"}))
        self.provider = {"base_url": "https://provider.example", "key_env": "RECORD_TEST_AUTH_TOKEN",
                         "credentials_from": "claude_settings", "format": "anthropic", "models": {"opus": "Opus"}}

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def live(self, key="current-provider-key", base="https://provider.example/"):
        (self.root / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_BASE_URL": base}}), encoding="utf-8")

    def test_current_same_endpoint_credential_overrides_stale_environment(self):
        self.live()
        self.assertEqual(llm._base_and_key(self.provider), ("https://provider.example", "current-provider-key"))

    def test_switching_to_other_provider_does_not_reuse_its_key(self):
        self.live(base="https://other.example")
        self.assertEqual(llm._base_and_key(self.provider)[1], "saved-provider-key")

    def test_missing_or_partially_written_live_config_retains_saved_key(self):
        self.assertEqual(llm._base_and_key(self.provider)[1], "saved-provider-key")
        (self.root / ".claude" / "settings.json").write_text("{", encoding="utf-8")
        self.assertEqual(llm._base_and_key(self.provider)[1], "saved-provider-key")

    def test_provider_without_sync_keeps_its_own_credential(self):
        self.live()
        self.provider.pop("credentials_from")
        self.assertEqual(llm._base_and_key(self.provider)[1], "saved-provider-key")

    def test_rotating_key_creates_new_client_and_preserves_inflight_client(self):
        import anthropic

        self.live()
        with patch.object(llm, "_clients", {}), patch.object(anthropic, "Anthropic") as factory:
            first = llm._client("test", self.provider)
            self.assertIs(llm._client("test", self.provider), first)
            self.assertEqual(factory.call_count, 1)
            self.live(key="rotated-provider-key")
            llm._client("test", self.provider)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(factory.call_args.kwargs["auth_token"], "rotated-provider-key")
            first.close.assert_not_called()

    def test_rotating_key_refreshes_model_availability_before_cache_expires(self):
        self.live()
        with patch.dict(CFG, {"llm": {"providers": {"test": self.provider}}}), \
             patch.object(llm, "_avail", {"at": 0, "ids": {}}), \
             patch.object(llm, "_fetch_ids", side_effect=[set(), {"opus"}]) as fetch:
            self.assertFalse(llm.availability()["test:opus"])
            self.assertFalse(llm.availability()["test:opus"])
            self.assertEqual(fetch.call_count, 1)
            self.live(key="rotated-provider-key")
            self.assertTrue(llm.availability()["test:opus"])
            self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
