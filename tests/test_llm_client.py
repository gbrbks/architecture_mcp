import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "archie" / "standalone"))
import llm_client


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _write_config(tmp_path, cfg):
    d = tmp_path / ".archie"
    d.mkdir(exist_ok=True)
    (d / "models.json").write_text(json.dumps(cfg))


class TestResolveConfig:
    def test_no_keys_no_config_returns_none(self, tmp_path):
        assert llm_client.resolve_config(tmp_path, env={}) is None

    def test_anthropic_key_only_selects_anthropic_backend(self, tmp_path):
        cfg = llm_client.resolve_config(tmp_path, env={"ANTHROPIC_API_KEY": "sk-a"})
        assert cfg["backend"] == "anthropic"
        assert cfg["api_key"] == "sk-a"
        assert cfg["base_url"] == llm_client.ANTHROPIC_URL
        assert cfg["models"] == llm_client.ANTHROPIC_MODELS
        assert cfg["models"]["haiku"] == "claude-haiku-4-5"

    def test_openrouter_key_wins_over_anthropic(self, tmp_path):
        cfg = llm_client.resolve_config(
            tmp_path, env={"ANTHROPIC_API_KEY": "sk-a", "OPENROUTER_API_KEY": "sk-or"})
        assert cfg["backend"] == "openai"
        assert cfg["api_key"] == "sk-or"
        assert cfg["base_url"] == llm_client.OPENROUTER_URL
        assert cfg["models"] == llm_client.OPENROUTER_MODELS

    def test_config_file_openrouter(self, tmp_path):
        _write_config(tmp_path, {
            "provider": "openrouter",
            "models": {"haiku": "google/gemini-2.5-flash"},
        })
        cfg = llm_client.resolve_config(tmp_path, env={"OPENROUTER_API_KEY": "sk-or"})
        assert cfg["backend"] == "openai"
        assert cfg["models"]["haiku"] == "google/gemini-2.5-flash"
        # unspecified tiers fall back to provider defaults
        assert cfg["models"]["opus"] == llm_client.OPENROUTER_MODELS["opus"]

    def test_config_file_generic_openai_requires_base_url(self, tmp_path):
        _write_config(tmp_path, {"provider": "openai", "api_key_env": "MY_KEY"})
        assert llm_client.resolve_config(tmp_path, env={"MY_KEY": "k"}) is None  # no base_url
        _write_config(tmp_path, {"provider": "openai", "base_url": "http://localhost:11434/v1",
                                 "api_key_env": "MY_KEY"})
        cfg = llm_client.resolve_config(tmp_path, env={"MY_KEY": "k"})
        assert cfg["backend"] == "openai"
        assert cfg["base_url"] == "http://localhost:11434/v1"

    def test_enabled_false_disables(self, tmp_path):
        _write_config(tmp_path, {"provider": "openrouter", "enabled": False})
        assert llm_client.resolve_config(
            tmp_path, env={"OPENROUTER_API_KEY": "sk-or"}) is None

    def test_missing_key_for_configured_provider_returns_none(self, tmp_path):
        _write_config(tmp_path, {"provider": "openrouter"})
        assert llm_client.resolve_config(tmp_path, env={}) is None

    def test_env_overrides_beat_config_file(self, tmp_path):
        _write_config(tmp_path, {"provider": "anthropic"})
        cfg = llm_client.resolve_config(tmp_path, env={
            "ANTHROPIC_API_KEY": "sk-a",
            "ARCHIE_LLM_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "sk-or",
            "ARCHIE_LLM_BASE_URL": "https://gw.example/v1",
            "ARCHIE_MODEL_HAIKU": "meta-llama/llama-4-scout",
        })
        assert cfg["backend"] == "openai"
        assert cfg["base_url"] == "https://gw.example/v1"
        assert cfg["models"]["haiku"] == "meta-llama/llama-4-scout"
        assert cfg["api_key"] == "sk-or"

    def test_api_key_env_override(self, tmp_path):
        cfg = llm_client.resolve_config(tmp_path, env={
            "ARCHIE_LLM_PROVIDER": "openrouter",
            "ARCHIE_LLM_API_KEY_ENV": "CUSTOM_KEY", "CUSTOM_KEY": "sk-c"})
        assert cfg["api_key"] == "sk-c"

    def test_malformed_config_file_ignored(self, tmp_path):
        d = tmp_path / ".archie"
        d.mkdir()
        (d / "models.json").write_text("{not json")
        cfg = llm_client.resolve_config(tmp_path, env={"ANTHROPIC_API_KEY": "sk-a"})
        assert cfg["backend"] == "anthropic"

    def test_ci_ignores_file_api_key_env_and_base_url(self, tmp_path):
        # Attacker-controlled .archie/models.json in a PR-head checkout must not
        # be able to redirect the request or point api_key_env at a CI secret.
        _write_config(tmp_path, {
            "provider": "openrouter",
            "api_key_env": "GITHUB_TOKEN",
            "base_url": "https://attacker.example",
        })
        cfg = llm_client.resolve_config(tmp_path, env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghtok",
            "OPENROUTER_API_KEY": "sk-or",
        })
        assert cfg is not None
        assert cfg["base_url"] == llm_client.OPENROUTER_URL
        assert cfg["api_key"] == "sk-or"

    def test_ci_ignores_file_api_key_env_no_fallback_to_file_key(self, tmp_path):
        _write_config(tmp_path, {
            "provider": "openrouter",
            "api_key_env": "GITHUB_TOKEN",
        })
        cfg = llm_client.resolve_config(tmp_path, env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghtok",
        })
        assert cfg is None

    def test_ci_still_honors_archie_llm_base_url_env_override(self, tmp_path):
        _write_config(tmp_path, {
            "provider": "openai",
            "api_key_env": "GITHUB_TOKEN",
            "base_url": "https://attacker.example",
        })
        cfg = llm_client.resolve_config(tmp_path, env={
            "GITHUB_ACTIONS": "true",
            "ARCHIE_LLM_BASE_URL": "https://trusted-gateway.example/v1",
            "ARCHIE_LLM_API_KEY_ENV": "ARCHIE_LLM_API_KEY",
            "ARCHIE_LLM_API_KEY": "sk-trusted",
        })
        assert cfg is not None
        assert cfg["base_url"] == "https://trusted-gateway.example/v1"
        assert cfg["api_key"] == "sk-trusted"

    def test_non_ci_still_honors_file_api_key_env_and_base_url(self, tmp_path):
        _write_config(tmp_path, {
            "provider": "openai",
            "api_key_env": "MY_KEY",
            "base_url": "http://localhost:11434/v1",
        })
        cfg = llm_client.resolve_config(tmp_path, env={"MY_KEY": "k"})
        assert cfg is not None
        assert cfg["base_url"] == "http://localhost:11434/v1"
        assert cfg["api_key"] == "k"


class FakeTransport:
    """Captures request bodies; replays scripted responses."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []  # list of (url, headers, body_dict)

    def __call__(self, url, body, headers, timeout, max_retries):
        self.requests.append((url, headers, json.loads(body.decode())))
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


ANTH_CFG = {"backend": "anthropic", "base_url": llm_client.ANTHROPIC_URL,
            "api_key": "sk-a", "models": dict(llm_client.ANTHROPIC_MODELS)}


class TestAnthropicBackend:
    def test_plain_completion(self, monkeypatch):
        t = FakeTransport([{"content": [{"type": "text", "text": "hello"}],
                            "stop_reason": "end_turn"}])
        monkeypatch.setattr(llm_client, "_post_json", t)
        out = llm_client.complete("hi", tier="sonnet", config=ANTH_CFG)
        assert out["text"] == "hello"
        assert out["tool_calls"] == []
        url, headers, body = t.requests[0]
        assert url == llm_client.ANTHROPIC_URL
        assert headers["x-api-key"] == "sk-a"
        assert headers["anthropic-version"] == llm_client.ANTHROPIC_VERSION
        assert body["model"] == "claude-sonnet-4-6"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert "tools" not in body

    def test_forced_tool_choice(self, monkeypatch):
        tool = {"name": "emit_findings", "description": "d",
                "input_schema": {"type": "object", "properties": {}}}
        t = FakeTransport([{"content": [{"type": "tool_use", "name": "emit_findings",
                                         "input": {"findings": [1]}}],
                            "stop_reason": "tool_use"}])
        monkeypatch.setattr(llm_client, "_post_json", t)
        out = llm_client.complete("go", system="sys", tools=[tool],
                                  tool_choice="emit_findings", config=ANTH_CFG)
        assert out["tool_calls"] == [{"name": "emit_findings", "input": {"findings": [1]}}]
        _, _, body = t.requests[0]
        assert body["system"] == "sys"
        assert body["tool_choice"] == {"type": "tool", "name": "emit_findings"}
        assert body["tools"] == [tool]

    def test_tool_loop_executes_and_returns_final_text(self, monkeypatch):
        tool = {"name": "grep", "description": "d",
                "input_schema": {"type": "object", "properties": {}}}
        t = FakeTransport([
            {"content": [{"type": "tool_use", "id": "t1", "name": "grep",
                          "input": {"pattern": "x"}}],
             "stop_reason": "tool_use"},
            {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"},
        ])
        monkeypatch.setattr(llm_client, "_post_json", t)
        calls = []
        out = llm_client.complete(
            "go", tools=[tool], config=ANTH_CFG,
            tool_executor=lambda name, args: calls.append((name, args)) or "hit")
        assert out["text"] == "done"
        assert calls == [("grep", {"pattern": "x"})]
        # second request carries the tool_result back
        _, _, body2 = t.requests[1]
        assert body2["messages"][-1]["content"][0]["type"] == "tool_result"
        assert body2["messages"][-1]["content"][0]["content"] == "hit"

    def test_tool_loop_caps_turns(self, monkeypatch):
        tool_use = {"content": [{"type": "tool_use", "id": "t", "name": "grep",
                                 "input": {}}, {"type": "text", "text": "partial"}],
                    "stop_reason": "tool_use"}
        t = FakeTransport([tool_use, tool_use])
        monkeypatch.setattr(llm_client, "_post_json", t)
        out = llm_client.complete("go", tools=[{"name": "grep", "description": "",
                                                "input_schema": {}}],
                                  config=ANTH_CFG, max_turns=2,
                                  tool_executor=lambda n, a: "x")
        assert out["text"] == "partial"  # degrades to last seen text

    def test_tool_loop_skips_exec_on_final_capped_turn(self, monkeypatch):
        tool_use = {"content": [{"type": "tool_use", "id": "t", "name": "grep",
                                 "input": {}}, {"type": "text", "text": "partial"}],
                    "stop_reason": "tool_use"}
        t = FakeTransport([tool_use])
        monkeypatch.setattr(llm_client, "_post_json", t)
        executor_calls = []
        def track_executor(name, args):
            executor_calls.append((name, args))
            return "x"
        out = llm_client.complete("go", tools=[{"name": "grep", "description": "",
                                                "input_schema": {}}],
                                  config=ANTH_CFG, max_turns=1,
                                  tool_executor=track_executor)
        assert executor_calls == []  # executor was never called
        assert out["text"] == "partial"  # degrades to partial text
        assert out["tool_calls"] == []  # no tool calls in response

    def test_no_provider_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm_client, "resolve_config", lambda *a, **k: None)
        with pytest.raises(llm_client.LLMError):
            llm_client.complete("hi")


OAI_CFG = {"backend": "openai", "base_url": llm_client.OPENROUTER_URL,
           "api_key": "sk-or", "models": dict(llm_client.OPENROUTER_MODELS)}


class TestOpenAIBackend:
    def test_plain_completion(self, monkeypatch):
        t = FakeTransport([{"choices": [{"message": {"content": "hello"},
                                         "finish_reason": "stop"}]}])
        monkeypatch.setattr(llm_client, "_post_json", t)
        out = llm_client.complete("hi", system="sys", tier="haiku", config=OAI_CFG)
        assert out["text"] == "hello"
        url, headers, body = t.requests[0]
        assert url == llm_client.OPENROUTER_URL + "/chat/completions"
        assert headers["Authorization"] == "Bearer sk-or"
        assert body["model"] == "anthropic/claude-haiku-4.5"
        assert body["messages"][0] == {"role": "system", "content": "sys"}
        assert body["messages"][1] == {"role": "user", "content": "hi"}

    def test_tool_translation_and_forced_choice(self, monkeypatch):
        tool = {"name": "emit_findings", "description": "d",
                "input_schema": {"type": "object", "properties": {}}}
        t = FakeTransport([{"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {
                "name": "emit_findings",
                "arguments": "{\"findings\": [1]}"}}]},
            "finish_reason": "tool_calls"}]}])
        monkeypatch.setattr(llm_client, "_post_json", t)
        out = llm_client.complete("go", tools=[tool], tool_choice="emit_findings",
                                  config=OAI_CFG)
        assert out["tool_calls"] == [{"name": "emit_findings", "input": {"findings": [1]}}]
        _, _, body = t.requests[0]
        assert body["tools"] == [{"type": "function", "function": {
            "name": "emit_findings", "description": "d",
            "parameters": {"type": "object", "properties": {}}}}]
        assert body["tool_choice"] == {"type": "function",
                                       "function": {"name": "emit_findings"}}

    def test_tool_loop(self, monkeypatch):
        tool = {"name": "grep", "description": "d",
                "input_schema": {"type": "object", "properties": {}}}
        t = FakeTransport([
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "function": {"name": "grep",
                                          "arguments": "{\"pattern\": \"x\"}"}}]},
              "finish_reason": "tool_calls"}]},
            {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]},
        ])
        monkeypatch.setattr(llm_client, "_post_json", t)
        out = llm_client.complete("go", tools=[tool], config=OAI_CFG,
                                  tool_executor=lambda n, a: "hit")
        assert out["text"] == "done"
        _, _, body2 = t.requests[1]
        assert body2["messages"][-1] == {"role": "tool", "tool_call_id": "c1",
                                         "content": "hit"}
        # assistant turn (with its tool_calls) is echoed back before the tool msg
        assert body2["messages"][-2]["role"] == "assistant"

    def test_malformed_tool_arguments_degrade(self, monkeypatch):
        t = FakeTransport([{"choices": [{"message": {
            "content": "text anyway",
            "tool_calls": [{"id": "c1", "function": {"name": "grep",
                                                     "arguments": "{broken"}}]},
            "finish_reason": "tool_calls"}]}])
        monkeypatch.setattr(llm_client, "_post_json", t)
        out = llm_client.complete("go", config=OAI_CFG)  # no executor: single turn
        assert out["text"] == "text anyway"
        assert out["tool_calls"] == [{"name": "grep", "input": {}}]

    def test_tool_arguments_already_dict(self, monkeypatch):
        t = FakeTransport([{"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {
                "name": "emit_findings",
                "arguments": {"findings": []}}}]},
            "finish_reason": "tool_calls"}]}])
        monkeypatch.setattr(llm_client, "_post_json", t)
        out = llm_client.complete("go", config=OAI_CFG)
        assert out["tool_calls"] == [{"name": "emit_findings", "input": {"findings": []}}]

    def test_empty_choices_raises(self, monkeypatch):
        t = FakeTransport([{"choices": []}])
        monkeypatch.setattr(llm_client, "_post_json", t)
        with pytest.raises(llm_client.LLMError):
            llm_client.complete("hi", config=OAI_CFG)

    def test_tool_loop_skips_exec_on_final_capped_turn(self, monkeypatch):
        tool_use = {"choices": [{"message": {"content": "partial", "tool_calls": [
            {"id": "c1", "function": {"name": "grep", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}
        t = FakeTransport([tool_use])
        monkeypatch.setattr(llm_client, "_post_json", t)
        executor_calls = []
        def track_executor(name, args):
            executor_calls.append((name, args))
            return "x"
        out = llm_client.complete("go", tools=[{"name": "grep", "description": "",
                                                "input_schema": {}}],
                                  config=OAI_CFG, max_turns=1,
                                  tool_executor=track_executor)
        assert executor_calls == []  # executor was never called
        assert out["text"] == "partial"  # degrades to partial text
        assert out["tool_calls"] == []  # no tool calls in response


class TestRetry:
    def _http_error(self, code, retry_after=None):
        import io
        import urllib.error
        headers = {"Retry-After": retry_after} if retry_after else {}
        class H(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)
        return urllib.error.HTTPError("u", code, "err", H(headers), io.BytesIO(b"boom"))

    def test_retries_on_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
        ok = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        err = self._http_error(429)
        seq = [err, ok]
        def fake_urlopen(req, timeout):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return _Resp(item)
        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
        out = llm_client.complete("hi", config=ANTH_CFG)
        assert out["text"] == "ok"

    def test_exhausted_retries_raise_llmerror(self, monkeypatch):
        monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
        def fake_urlopen(req, timeout):
            raise self._http_error(503)
        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(llm_client.LLMError):
            llm_client.complete("hi", config=ANTH_CFG, max_retries=2)

    def test_400_does_not_retry(self, monkeypatch):
        calls = {"n": 0}
        def fake_urlopen(req, timeout):
            calls["n"] += 1
            raise self._http_error(400)
        monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(llm_client.LLMError):
            llm_client.complete("hi", config=ANTH_CFG, max_retries=3)
        assert calls["n"] == 1
