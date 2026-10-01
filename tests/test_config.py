"""Offline dotenv-path and process-environment precedence checks."""

from pathlib import Path
import runpy
import shutil

import pytest


@pytest.mark.parametrize("working_directory", [".", "backend", "unrelated"])
def test_repository_dotenv_is_independent_of_working_directory(tmp_path, monkeypatch, working_directory):
    source = Path(__file__).resolve().parents[1] / "backend" / "core" / "config.py"
    copied = tmp_path / "backend" / "core" / "config.py"
    copied.parent.mkdir(parents=True)
    shutil.copyfile(source, copied)
    (tmp_path / ".env").write_text(
        "AI_FOUNDRY_ENDPOINT=https://synthetic.example.test/\n"
        "LLM_ENDPOINT=https://text.example.test/\n"
        "LLM_DEPLOYMENT=synthetic-deployment\n"
    )
    working_path = tmp_path / working_directory
    working_path.mkdir(exist_ok=True)
    monkeypatch.chdir(working_path)
    monkeypatch.delenv("AI_FOUNDRY_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LLM_DEPLOYMENT", raising=False)

    settings = runpy.run_path(str(copied))["settings"]

    assert settings.AI_FOUNDRY_ENDPOINT == "https://synthetic.example.test/"
    assert settings.LLM_ENDPOINT == "https://text.example.test/"
    assert settings.LLM_DEPLOYMENT == "synthetic-deployment"


def test_process_environment_overrides_repository_dotenv(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "backend" / "core" / "config.py"
    copied = tmp_path / "backend" / "core" / "config.py"
    copied.parent.mkdir(parents=True)
    shutil.copyfile(source, copied)
    (tmp_path / ".env").write_text(
        "AI_FOUNDRY_ENDPOINT=https://dotenv.example.test/\n"
        "LLM_ENDPOINT=https://dotenv-text.example.test/\n"
        "LLM_DEPLOYMENT=dotenv-deployment\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_FOUNDRY_ENDPOINT", "https://process.example.test/")
    monkeypatch.setenv("LLM_ENDPOINT", "https://process-text.example.test/")
    monkeypatch.setenv("LLM_DEPLOYMENT", "process-deployment")

    settings = runpy.run_path(str(copied))["settings"]

    assert settings.AI_FOUNDRY_ENDPOINT == "https://process.example.test/"
    assert settings.LLM_ENDPOINT == "https://process-text.example.test/"
    assert settings.LLM_DEPLOYMENT == "process-deployment"


@pytest.mark.parametrize("explicit,text,shared,expected", [
    ("https://explicit.example.test/", "https://text.example.test/", "https://shared.example.test/", "https://explicit.example.test/"),
    (None, "https://text.example.test/", "https://shared.example.test/", "https://text.example.test/"),
    (None, "https://text.example.test/", None, "https://text.example.test/"),
    (None, None, "https://shared.example.test/", "https://shared.example.test/"),
    (None, "", "https://shared.example.test/", "https://shared.example.test/"),
])
def test_text_endpoint_selection_does_not_mutate_shared_settings(monkeypatch, explicit, text, shared, expected):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock
    import openai

    settings = SimpleNamespace(LLM_ENDPOINT=text, AI_FOUNDRY_ENDPOINT=shared, LLM_DEPLOYMENT="synthetic")
    monkeypatch.setitem(sys.modules, "backend.core.config", SimpleNamespace(settings=settings))
    sync_client, async_client = Mock(), Mock()
    monkeypatch.setattr(openai, "AzureOpenAI", sync_client)
    monkeypatch.setattr(openai, "AsyncAzureOpenAI", async_client)
    source = Path(__file__).resolve().parents[1] / "backend" / "core" / "llm.py"
    client_type = runpy.run_path(str(source))["LLMClient"]
    token_provider = Mock(side_effect=AssertionError("Offline endpoint test must not request a token"))

    client = client_type(endpoint=explicit, token_provider=token_provider)

    assert client.endpoint == expected
    assert settings.AI_FOUNDRY_ENDPOINT == shared
    for constructor in (sync_client, async_client):
        assert constructor.call_args.kwargs["azure_endpoint"] == expected
        assert constructor.call_args.kwargs["azure_ad_token_provider"] is token_provider
    token_provider.assert_not_called()