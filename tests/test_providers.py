import io
import json
import urllib.error
import urllib.request

import pytest

from xharness.config import load_config
from xharness.providers import PRESETS, apply_preset, probe_provider


def test_preset_fills_defaults_and_explicit_keys_win():
    merged = apply_preset({"preset": "ollama", "model": "m"})
    assert merged["base_url"] == PRESETS["ollama"]["base_url"] and merged["model"] == "m"
    assert "preset" not in merged and "local" not in merged and "notes" not in merged
    merged = apply_preset({"preset": "openai", "model": "m", "base_url": "https://proxy.example/v1", "api_key_env": "MY_KEY"})
    assert merged["base_url"] == "https://proxy.example/v1" and merged["api_key_env"] == "MY_KEY"
    with pytest.raises(KeyError, match="unknown provider preset"):
        apply_preset({"preset": "nope", "model": "m"})


def test_no_preset_passes_through_known_keys_only():
    merged = apply_preset({"base_url": "http://x/v1", "model": "m", "junk": 1})
    assert merged == {"base_url": "http://x/v1", "model": "m"}


def test_load_config_applies_presets_and_requires_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "xharness.toml").write_text('default_provider = "farm"\n[providers.farm]\npreset = "llama-cpp"\nmodel = "taide"\n[providers.cloud]\npreset = "groq"\nmodel = "x"\n', encoding="utf-8")
    config = load_config()
    assert config.provider["base_url"] == "http://127.0.0.1:8080/v1" and config.provider["model"] == "taide"
    assert config.providers["cloud"]["api_key_env"] == "GROQ_API_KEY"
    (tmp_path / "xharness.toml").write_text('[providers.bad]\npreset = "ollama"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no model"):
        load_config()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_probe_lists_models_and_reports_failures(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return FakeResponse(json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("K", "secret")
    result = probe_provider("p", {"base_url": "http://h/v1/", "api_key_env": "K"})
    assert result.ok and result.models == ["m1", "m2"] and captured["url"] == "http://h/v1/models"
    assert captured["auth"] == "Bearer secret"

    def failing(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "nope", hdrs=None, fp=io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", failing)
    result = probe_provider("p", {"base_url": "http://h/v1"})
    assert not result.ok and "401" in result.detail
    assert not probe_provider("p", {"base_url": "file:///etc"}).ok
    assert not probe_provider("p", {}).ok


def test_cli_presets_needs_no_config(tmp_path, monkeypatch, capsys):
    from xharness.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XHARNESS_BASE_URL", raising=False)
    assert main(["providers", "presets"]) == 0
    out = capsys.readouterr().out
    assert "ollama" in out and "hosted" in out and "OPENAI_API_KEY" in out
