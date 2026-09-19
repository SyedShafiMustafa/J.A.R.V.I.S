from agents.llm_provider import build_llm_provider


class JarvisBrain:
    """JARVIS chat brain.

    Delegates to the configured LLM provider (Ollama locally, or an
    OpenAI-compatible endpoint when configured in .env). The public API
    (`stream`, `ask`) and Ollama-by-default behavior are unchanged.
    """

    def __init__(self):
        self.provider = build_llm_provider()
        # Back-compat attributes some callers/tests read.
        self.url = getattr(self.provider, "url", None)
        self.model = getattr(self.provider, "model", None)

    def stream(self, messages: list[dict]):
        yield from self.provider.stream(messages)

    def ask(self, messages: list[dict]):
        return " ".join(self.stream(messages))
