import pytest


@pytest.fixture(autouse=True)
def _offline_models(monkeypatch):
    # Never ask the machine's real `agy` (a network call) which models it has.
    monkeypatch.setenv("AGYTEAM_MODEL_CHECK", "0")
    from agyteam import runner
    runner._MODEL_CACHE.clear()
