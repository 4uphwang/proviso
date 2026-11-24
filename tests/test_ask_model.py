import dotenv

from proviso.agent.ask import ASK_MODEL, pick_model
from proviso.agent.run import DEFAULT_MODEL


def test_explicit_model_wins():
    assert pick_model("qwen3.5:9b") == "qwen3.5:9b"


def test_default_uses_api_when_key(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    assert pick_model(None) == ASK_MODEL


def test_default_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert pick_model(None) == DEFAULT_MODEL
