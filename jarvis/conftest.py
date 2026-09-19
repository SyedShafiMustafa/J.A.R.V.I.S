"""Test isolation for the J.A.R.V.I.S. suite.

`.env` can point J.A.R.V.I.S. at a real cloud brain (Groq) for normal use.
Unit tests must never depend on — or accidentally call — a paid network
endpoint: several suites patch the *local* Ollama client and would otherwise
leak real requests to the configured cloud provider.

Every test therefore runs with the local provider selected. A test that wants
to exercise provider selection overrides these values itself (see
``agents/tests/test_llm_provider.py``).
"""

import pytest


@pytest.fixture(autouse=True)
def _local_llm_by_default(monkeypatch):
    from agents import llm_provider

    monkeypatch.setattr(llm_provider, "LLM_PROVIDER", "ollama", raising=False)
    monkeypatch.setattr(llm_provider, "LLM_FALLBACK", False, raising=False)


collect_ignore = ["scripts", "run_voice_test.py"]
