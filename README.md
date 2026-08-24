SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT NOT NULL, user_id TEXT NOT NULL, conversation_id TEXT, stage TEXT,
    vars TEXT, question TEXT, answer TEXT, session_id TEXT, seq INTEGER,
    title TEXT, created_at TEXT, updated_at TEXT,
    job_id TEXT, job_status TEXT, job_message TEXT, job_output TEXT,
    job_baseline TEXT, job_started_at TEXT,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS IX_conversations_user_session
    ON conversations (user_id, session_id);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT NOT NULL, session_id TEXT, user_id TEXT NOT NULL,
    current_conversation_id TEXT, title TEXT, created_at TEXT, updated_at TEXT,
    PRIMARY KEY (user_id, id)
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    user_id TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT,
    PRIMARY KEY (user_id, conversation_id, seq)
);
-- Internal trace of agent Function App calls (see dbo.agent_calls in schema.sql for
-- the full rationale). SQLite has no IDENTITY or BIT: AUTOINCREMENT assigns the id,
-- and is_error is an INTEGER holding 0/1 -- `is_error = 1` reads the same on both.
CREATE TABLE IF NOT EXISTS agent_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, conversation_id TEXT NOT NULL, agent TEXT NOT NULL,
    request_text TEXT, response_json TEXT,
    duration_ms INTEGER NOT NULL,
    is_error INTEGER NOT NULL DEFAULT 0, error_text TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS IX_agent_calls_conversation
    ON agent_calls (user_id, conversation_id, id);
CREATE INDEX IF NOT EXISTS IX_agent_calls_agent_time
    ON agent_calls (agent, created_at);
"""
