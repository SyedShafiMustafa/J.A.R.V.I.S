import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.memory import Memory
from agents.brain import JarvisBrain

memory = Memory()
brain = JarvisBrain()

print("🤖 JARVIS Memory Test\n")

# First message (this gets remembered)
user1 = input("You (teach Jarvis something): ")
reply1 = brain.ask(user1)
memory.save_memory(user1, reply1)

print("Jarvis:", reply1)

print("\n---------------------------\n")

# Follow-up question
user2 = input("You (ask something): ")

memories = memory.search_memories(user2)

if memories:
    context = f"Previous memory: {memories}\n\nUser: {user2}"
else:
    context = user2

reply2 = brain.ask(context)

print("\nJarvis:", reply2)