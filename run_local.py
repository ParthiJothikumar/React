Here's the actual code for every change.

1. db.py — _SQLITE_SCHEMA, added table after sessions

CREATE TABLE IF NOT EXISTS conversation_turns (
    user_id TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT,
    PRIMARY KEY (user_id, conversation_id, seq)
);
2. setup_sqlite.py — added after sessions table

    -- FE-visible transcript, one row per shown message (Approach A). The
    -- conversations API replays these instead of the raw Foundry item stream.
    CREATE TABLE IF NOT EXISTS conversation_turns (
        user_id                   TEXT    NOT NULL,
        conversation_id           TEXT    NOT NULL,
        seq                       INTEGER NOT NULL,   -- order shown to the user
        role                      TEXT    NOT NULL,   -- user | assistant
        content                   TEXT    NOT NULL,
        created_at                TEXT,
        PRIMARY KEY (user_id, conversation_id, seq)
    );
3. schema.sql — added after dbo.sessions (Azure SQL — run this manually)

-- FE-visible transcript: one row per message actually shown to the user
-- (Approach A). The /conversations API replays these rows in `seq` order
-- instead of reconstructing history from the agents' raw Foundry item stream.
IF OBJECT_ID('dbo.conversation_turns', 'U') IS NULL
CREATE TABLE dbo.conversation_turns (
    user_id                   NVARCHAR(200)  NOT NULL,
    conversation_id           NVARCHAR(200)  NOT NULL,
    seq                       INT            NOT NULL,   -- order shown to the user
    role                      NVARCHAR(20)   NOT NULL,   -- user | assistant
    content                   NVARCHAR(MAX)  NOT NULL,
    created_at                NVARCHAR(50)   NULL,
    CONSTRAINT PK_conversation_turns PRIMARY KEY (user_id, conversation_id, seq)
);
GO
4. persistence.py — two new functions inserted before load_session

def append_turns(conn, user_id: str, conversation_id: str, turns) -> None:
    """Append FE-visible messages to conversation_turns (Approach A).

    `turns` is a list of (role, content) pairs in display order -- typically the
    user's message followed by each assistant line we return that turn. This is the
    EXACT text shown to the user (post-translation, no agent JSON), so the
    conversations API can replay it verbatim. Blank/whitespace-only contents are
    skipped. Each row gets the next sequential seq for this conversation, so history
    always reads back in the order the user saw it.
    """
    rows = [(role, content) for role, content in turns if content and content.strip()]
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    # Next seq = one past the current max for this conversation (0 on first write).
    cur.execute(
        "SELECT MAX(seq) FROM conversation_turns WHERE user_id = ? AND conversation_id = ?",
        (user_id, conversation_id),
    )
    row = cur.fetchone()
    next_seq = (row[0] + 1) if row and row[0] is not None else 0
    for role, content in rows:
        cur.execute(
            "INSERT INTO conversation_turns "
            "(user_id, conversation_id, seq, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, conversation_id, next_seq, role, content, now),
        )
        next_seq += 1
    conn.commit()


def load_turns(conn, user_id: str, conversation_id: str) -> list:
    """Return a conversation's FE-visible transcript as [{role, content}], oldest
    first. Empty list when nothing was recorded (e.g. chats created before Approach
    A) -- the caller falls back to shaping the raw Foundry stream in that case.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM conversation_turns "
        "WHERE user_id = ? AND conversation_id = ? ORDER BY seq ASC",
        (user_id, conversation_id),
    )
    return [{"role": r[0], "content": r[1]} for r in cur.fetchall()]
5. routes.py — six edits
(a) line 9 — import


import json
(b) lines 43-50 — import the new helpers


from app.persistence import (
    append_turns,
    load_conversation,
    load_session,
    load_turns,
    save_conversation,
    save_session,
)
(c) lines 88-136 — the Foundry fallback shaper (new)


# role:"user" items that are actually tool results fed back to the agents (not typed
# by a human). Extend as agents add new injected-context sentinels.
_INTERNAL_USER_PREFIXES = ("KB_SEARCH_RESULTS",)


def _shape_foundry_messages(raw_messages, final_answer=None):
    """FALLBACK only: build a clean FE transcript from the raw Foundry item stream.

    Used for conversations created before Approach A (no rows in conversation_turns).
    The Foundry stream is the agents' shared scratchpad, so we drop tool-result
    injections and unwrap our JSON envelopes, keeping only the human-visible text
    (agent_message / steps / follow-up). It is inherently lossy -- server-authored
    lines and job results were never written to Foundry -- which is exactly why new
    chats are served from conversation_turns instead.
    """
    out = []
    for m in raw_messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            if content.startswith(_INTERNAL_USER_PREFIXES):
                continue  # tool-result injection, not a real user turn
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            text = content
            try:
                obj = json.loads(content)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                if obj.get("status") == "search":
                    continue  # internal "let me look that up" filler
                text = (
                    obj.get("agent_message")
                    or obj.get("steps")
                    or obj.get("follow_up_question")
                    or ""
                )
            if text:
                out.append({"role": "assistant", "content": text})
    # Foundry stored only agent_message for the resolved turn; the composed final
    # answer (steps + KB link) lives in SQL -- swap it in as the last assistant bubble.
    if final_answer and out and out[-1]["role"] == "assistant":
        out[-1]["content"] = final_answer
    return out
(d) lines 184-190 — inside /chat, right after save_conversation(...)


        # Record the FE-visible transcript for this turn (Approach A): the user's
        # message plus each assistant line we're returning, exactly as shown.
        append_turns(
            conn,
            request.user_id,
            conversation.id,
            [("user", request.message)] + [("assistant", m) for m in messages],
        )
(e) lines 272-278 — inside /chat/continue, right after save_conversation(...)


        append_turns(
            conn,
            request.user_id,
            conv_id,
            [("user", request.message)] + [("assistant", m) for m in messages],
        )
(f) lines 410-415 — inside /jobs/status completion branch, after save_conversation(...)


        # Job completion produces assistant-only lines (result + next question).
        # No user turn here -- the FE was polling, not chatting.
        append_turns(
            conn, user_id, conversation_id,
            [("assistant", m) for m in messages],
        )
(g) lines 515-538 — get_conversation body rewritten


        rows = _fetchall_dicts(cur)
        # Step 2: attach the FE-visible transcript we recorded turn-by-turn
        # (Approach A) -- exactly what the user saw, no agent JSON, nothing missing.
        for row in rows:
            row["messages"] = load_turns(conn, user_id, row["conversation_id"])
    except Exception:
        logger.exception("get_conversation failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load conversations")
    finally:
        conn.close()

    # Step 3: fallback for conversations created before Approach A (no recorded
    # turns) -- reconstruct a best-effort transcript from the raw Foundry stream.
    for row in rows:
        if row["messages"]:
            row["messages_ok"] = True
            continue
        try:
            raw = _conversation_messages(openai_client, row["conversation_id"])
            row["messages"] = _shape_foundry_messages(raw, row.get("answer"))
            row["messages_ok"] = True
        except Exception:
            logger.exception("load messages failed conv_id=%s", row.get("conversation_id"))
            row["messages"] = []
            row["messages_ok"] = False  # FE can flag "couldn't load this part"

    return {"user_id": user_id, "session_id": session_id, "conversations": rows}
That's the complete diff. The /jobs/status progress-poll branch (the transient "Working..." spinner lines) was intentionally left untouched — those aren't recorded as history.

