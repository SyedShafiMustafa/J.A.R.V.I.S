from core.memory import Memory

print("🧠 Testing JARVIS Memory...\n")

memory = Memory()

memory.save_memory(
    user_message="I love Python programming",
    assistant_message="I'll remember that you enjoy Python."
)

memory.save_memory(
    user_message="My favorite editor is VS Code",
    assistant_message="Got it. VS Code is your preferred editor."
)

print("✅ Memories saved.\n")

results = memory.search_memories("What programming language do I like?")

print("🔍 Search Results:\n")

for i, item in enumerate(results, 1):
    print(f"{i}. {item}")