import threading
import sqlite3

from config.settings import MEMORY_DB_PATH


class Memory:
    def __init__(self):
        self.db_path = MEMORY_DB_PATH

        self.conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False
        )

        self.lock = threading.Lock()
        self.cursor = self.conn.cursor()

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_message TEXT,
                assistant_message TEXT
            )
        """)
        self.conn.commit()

    def save_memory(self, user_message, assistant_message):
        with self.lock:
            self.cursor.execute(
                "INSERT INTO memories (user_message, assistant_message) VALUES (?, ?)",
                (user_message, assistant_message)
            )
            self.conn.commit()

    def search_memories(self, query):
        with self.lock:
            words = query.lower().replace("?", "").split()
            results = []

            for word in words:
                self.cursor.execute(
                    """
                    SELECT user_message, assistant_message
                    FROM memories
                    WHERE LOWER(user_message) LIKE ?
                    """,
                    (f"%{word}%",)
                )
                results.extend(self.cursor.fetchall())

        return list(dict.fromkeys(results))