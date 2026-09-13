class OllamaError(RuntimeError):
    """Base class for expected Ollama boundary failures."""

    user_message = "I'm having trouble reaching my brain. Please try again."


class OllamaUnavailableError(OllamaError):
    user_message = "Ollama is unavailable. Please check that it is running."


class OllamaTimeoutError(OllamaError):
    user_message = "Ollama took too long to respond. Please try again."


class OllamaMalformedResponseError(OllamaError):
    user_message = "Ollama returned an invalid response. Please try again."


class PlannerValidationError(OllamaError):
    user_message = "I couldn't safely interpret that action plan."
