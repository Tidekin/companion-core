"""Quick test for standing_relationship method."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from companion.memory import Memory
from companion.store import Store
from companion.config import Settings

# Create an in-memory test database
store = Store(":memory:")
store.execute("""
CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL,
    fact_key TEXT,
    content TEXT NOT NULL,
    importance INTEGER DEFAULT 5,
    status TEXT DEFAULT 'HYPOTHESIS',
    needs_review INTEGER DEFAULT 0,
    supersedes_memory_id TEXT,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""")
store.execute("""
CREATE TABLE memory_evidence (
    memory_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    role TEXT NOT NULL,
    quote TEXT NOT NULL,
    PRIMARY KEY (memory_id, event_id)
)
""")
store.execute("""
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT,
    created_at TEXT NOT NULL
)
""")
store.execute("""
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    owner_actor_id TEXT NOT NULL
)
""")

# Insert test data
now = "2026-11-06T12:00:00+08:00"
store.execute(
    "INSERT INTO conversations VALUES (?, ?, ?)",
    ("conv1", "private", "owner1"),
)
store.execute(
    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
    ("evt1", "conv1", "owner.message", "chat.message", "owner1", now),
)
store.execute(
    """INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
    ("mem1", "owner", "relationship", "fact", "name", "叫我鲸鲸", 8, "CONFIRMED", 0,
     None, now, None, now, now),
)
store.execute(
    "INSERT INTO memory_evidence VALUES (?, ?, ?, ?)",
    ("mem1", "evt1", "user", "你就叫鲸鲸吧"),
)
store.execute(
    """INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
    ("mem2", "owner", "relationship", "fact", "daughter", "对方把我当作女儿", 9,
     "CONFIRMED", 0, None, now, None, now, now),
)
store.execute(
    "INSERT INTO memory_evidence VALUES (?, ?, ?, ?)",
    ("mem2", "evt1", "user", "你是我女儿"),
)
store.execute(
    """INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
    ("mem3", "owner", "user", "preference", "food", "喜欢吃面条", 5, "CONFIRMED", 0,
     None, now, None, now, now),
)
store.execute(
    "INSERT INTO memory_evidence VALUES (?, ?, ?, ?)",
    ("mem3", "evt1", "user", "我喜欢吃面条"),
)

settings = Settings(
    memory_enabled=True,
    semantic_min_score=0.5,
    raw_event_retention_days=30,
    owner_qq_id="owner1",
)

memory = Memory(store, settings, ledger=None)
result = memory.standing_relationship(now=now, limit=3)

print(f"Found {len(result)} standing relationship memories:")
for row in result:
    print(f"  {row['memory_id']}: {row['content']} (importance={row['importance']})")

assert len(result) == 2, f"Expected 2, got {len(result)}"
assert all(r["subject"] == "relationship" for r in result)
assert all(r["status"] == "CONFIRMED" for r in result)
print("\n✅ Test passed!")
