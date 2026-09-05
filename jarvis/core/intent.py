ACTION_WORDS = [
    "open", "launch", "start", "run",
    "close", "quit",
    "write", "type",
    "press", "click",
    "scroll", "search",
    "create", "save",
    "copy", "paste"
]


class IntentClassifier:

    def classify(self, text: str):

        lower = text.lower()

        if any(word in lower for word in ACTION_WORDS):
            return "task"

        return "chat"