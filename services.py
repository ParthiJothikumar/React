"""Service layer: one class per use case group, each owning a whole unit of work.

Sits between routes.py (HTTP) and flow.py (the stage machine). A service method owns
the *sequence* of a use case -- run the flow, translate the reply, persist the turn,
dispatch any async job -- while knowing nothing about requests, responses or status
codes. It returns plain dicts and raises the framework-free errors in errors.py;
routes.py is the only module that turns those into HTTP.

Each service takes its collaborators in __init__ (repositories, clients, the flow) and
never reaches for a module global. That is what makes them testable: a test builds
ChatService(FakeConversationRepo(), FakeSessionRepo(), FakeFlow(), FakeFoundry()) and
drives any branch with no database, no Azure and no network.

TRANSACTION BOUNDARY: this layer owns it. Each service method does its slow work first
(flow handlers, agent calls, translation) and only then opens repo.transaction() around
the writes that must agree with each other -- so a crash can't leave the conversation
row advanced with its transcript rows missing, and no network call is ever inside an
open transaction holding row locks.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.constants import (
    DIAGNOSTICS_RUNNING,
    DONE,
    JOB_DONE,
    JOB_FAILED,
    JOB_KIND_DIAGNOSTIC,
    JOB_KIND_TROUBLESHOOT,
    JOB_TERMINAL,
    TROUBLESHOOT_RUNNING,
)
from app.config import Settings, logger
from app.errors import (
    AppError,
    ConversationEnded,
    NotFound,
    TurnFailed,
    UpstreamTransient,
)


# Cap on the raw device output we store, so one runaway script can't bloat a row.
MAX_JOB_OUTPUT = 8000


def _is_overdue(job_started_at, timeout_minutes: int) -> bool:
    """True when a job was triggered longer ago than we are willing to wait.

    Wall-clock, measured from the trigger -- which is the whole point. The previous rule
    counted polls, and a poll only happens while a browser is open, so a user who closed
    their tab never accumulated any and the conversation stayed RUNNING forever.

    A missing or unparseable timestamp returns False: better to keep waiting (the reaper
    will look again next sweep) than to fail a job that may be running perfectly well.
    """
    if not job_started_at:
        return False
    try:
        started = datetime.fromisoformat(str(job_started_at).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("unparseable job_started_at %r; not expiring", job_started_at)
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started > timedelta(minutes=timeout_minutes)


def _join(messages) -> str:
    """Collapse a turn's lines into the single `answer` string the FE also reads."""
    return "\n\n".join(msg for msg in messages if msg)


def _visible(messages) -> list:
    """Drop blank/whitespace-only lines.

    Applied BEFORE both the response and the transcript write, so the two cannot
    disagree: ConversationRepository.append_turns skips blank content, and without
    this the FE would render an empty bubble that vanished on refresh.
    """
    return [m for m in messages if m and m.strip()]


class ChatService:
    """Starting and continuing conversations (the command side)."""

    def __init__(self, conversations, sessions, flow, foundry, jobs, multilingual):
        self._conversations = conversations
        self._sessions = sessions
        self._flow = flow
        self._foundry = foundry
        self._jobs = jobs
        self._lang = multilingual

    def start_chat(self, user_id: str, message: str) -> dict:
        """Start a new chat session and run the first turn.

        Creates a fresh Foundry conversation and a new session id, runs the flow for the
        opening message, translates the reply into the user's language, then persists the
        conversation, transcript and session rows.
        """
        session_id = "sess_" + uuid.uuid4().hex
        conversation = self._foundry.create_conversation()
        # Handed to flow.step, which fills in interaction_id / incident_id as it goes.
        # We keep our own reference so a failure can still report what was raised: the
        # dict is mutated in place, so incident_id is visible here even if step() throws
        # afterwards.
        flow_vars = {"user_id": user_id}
        try:
            messages, stage, flow_vars = self._flow.step(
                conversation.id, message, None, flow_vars
            )
            messages, answer, pending_job = self._persist_turn(
                user_id, conversation.id,
                question=message, messages=messages, stage=stage, flow_vars=flow_vars,
                session_id=session_id, seq=0,
            )
            self._sessions.save(user_id, session_id, conversation.id, message)
            self._dispatch_pending_job(user_id, conversation.id, pending_job)
            return _chat_payload(
                user_id, session_id, conversation.id, stage, messages, answer
            )
        except (NotFound, ConversationEnded):
            raise
        except Exception as exc:
            # Nothing is saved on a failed first turn, so the user must start over -- and
            # if a ticket was already raised, starting over would open a SECOND one.
            # Carry the incident out so the controller can hand them the number instead
            # of inviting a retry.
            raise TurnFailed(
                None, conversation.id, exc,
                incident_id=flow_vars.get("incident_id"),
            ) from exc

    def continue_chat(self, user_id: str, session_id: str, message: str) -> dict:
        """Continue an ACTIVE conversation in a session by session_id.

        Loads the session's current conversation and advances it by one turn. If that
        conversation has already ended (stage=DONE) it is final -- we raise
        ConversationEnded so the frontend starts a fresh chat; there is no rollover and
        no memory carry-over.
        """
        session = self._sessions.load(user_id, session_id)
        if session is None:
            raise NotFound("Session not found")

        conv_id = session.get("current_conversation_id")
        conversation = self._conversations.load(user_id, conv_id)
        if conversation is None:
            raise NotFound("Conversation not found")

        stage = conversation.get("stage", DONE)
        flow_vars = conversation.get("vars", {})

        # Ended conversations are final -- no rollover, no memory carry-over. The
        # frontend should start a new chat instead of continuing.
        if stage == DONE:
            raise ConversationEnded(
                "This conversation has ended. Please start a new chat."
            )

        # Anything that fails below leaves the conversation exactly where it was (the
        # writes are one transaction), so it is retryable -- and TurnFailed carries the
        # stage out, because routes.py cannot know it from the exception alone.
        # `current_stage` tracks what is genuinely in the DB: the old stage before the
        # write commits, the new one after.
        current_stage = stage
        try:
            messages, new_stage, new_flow_vars = self._flow.step(
                conv_id, message, stage, flow_vars
            )
            messages, answer, pending_job = self._persist_turn(
                user_id, conv_id,
                question=message, messages=messages, stage=new_stage,
                flow_vars=new_flow_vars,
            )
            current_stage = new_stage  # the transaction committed; the stage has moved
            self._dispatch_pending_job(user_id, conv_id, pending_job)
        except ConversationEnded:
            raise  # terminal, not retryable -- the caller shows the "ended" message
        except Exception as exc:
            raise TurnFailed(current_stage, conv_id, exc) from exc
        return _chat_payload(user_id, session_id, conv_id, new_stage, messages, answer)

    # -- internals ----------------------------------------------------------
    def _persist_turn(
        self, user_id, conv_id, *, question, messages, stage, flow_vars,
        session_id=None, seq=None,
    ):
        """The tail every chat turn shares: translate, persist state, record transcript.

        Returns (messages, answer, pending_job). The caller dispatches `pending_job`
        once writing is done -- the conversation row must exist before a job_id can be
        recorded against it.
        """
        # If the flow asked to start an async job, pull the request out before saving so
        # it isn't persisted in vars; the caller dispatches it once the row exists.
        pending_job = flow_vars.pop("start_job", None)
        messages = _visible(
            self._lang.translate_messages(messages, flow_vars.get("lang"))
        )
        answer = _join(messages)
        # One transaction for both writes: the state and the transcript must agree, or a
        # crash between them leaves the stage advanced with the messages missing. Opened
        # here, AFTER the flow and translation calls, so no network call is inside it.
        with self._conversations.transaction():
            self._conversations.save(
                user_id, conv_id,
                stage=stage, flow_vars=flow_vars, question=question, answer=answer,
                session_id=session_id, seq=seq,
            )
            # Record the FE-visible transcript for this turn: the user's message plus
            # each assistant line we're returning, exactly as shown.
            self._conversations.append_turns(
                user_id, conv_id,
                [("user", question)] + [("assistant", m) for m in messages],
            )
        return messages, answer, pending_job

    def _dispatch_pending_job(self, user_id, conv_id, pending):
        """Start an async job the flow requested (via flow_vars["start_job"]).

        No queue, no background loop: we call the agent's /start to get a job_id and
        record it on the conversation row (job_status=running). From then on the FE
        polls GET /jobs/status. Runs AFTER the conversation row is saved, so the row
        exists to update.
        """
        if not pending:
            return
        job_id = self._jobs.start(
            pending["kind"], pending.get("start_url", ""), conv_id,
            pending.get("input", ""),
        )
        # Capture the baseline at trigger time so a later poll can tell OUR fresh Intune
        # run-state row apart from a stale one left by a previous run.
        self._conversations.start_job(
            user_id, conv_id, job_id, self._jobs.baseline_stamp()
        )


class JobService:
    """Advancing a running diagnostics/troubleshoot job (what the FE poll drives)."""

    def __init__(self, conversations, flow, jobs, multilingual, settings: Settings):
        self._conversations = conversations
        self._flow = flow
        self._jobs = jobs
        self._lang = multilingual
        self._settings = settings

    def advance_job(
        self, user_id: str, conversation_id: str, session_id: Optional[str] = None
    ) -> dict:
        """The FE spinner polls this every ~30s (no backend queue/loop).

        On each poll we look up the job_id by conversation_id, call the agent's /status
        LIVE, store the latest status in the DB, and:
          - still running -> return the progress with done=False (keep spinning)
          - done/failed   -> advance the flow ONCE (result + next question), persist the
                             new stage, and return done=True so the FE stops polling.
        """
        conversation = self._conversations.load(user_id, conversation_id)
        if conversation is None:
            raise NotFound("Conversation not found")
        stage = conversation.get("stage")
        flow_vars = conversation.get("vars", {})
        job = self._conversations.get_job(user_id, conversation_id)

        # Not in a running stage -> nothing to poll (not started, or already advanced).
        if stage not in (DIAGNOSTICS_RUNNING, TROUBLESHOOT_RUNNING):
            return _job_payload(
                user_id, session_id, conversation_id, stage,
                done=job.get("job_status") in JOB_TERMINAL,
                messages=[job.get("job_message") or "No active job."],
            )

        # Running -> which agent to ask, from the stage.
        if stage == DIAGNOSTICS_RUNNING:
            kind, status_url = (
                JOB_KIND_DIAGNOSTIC, self._settings.DIAGNOSTICS_STATUS_URL
            )
        else:
            kind, status_url = (
                JOB_KIND_TROUBLESHOOT, self._settings.TROUBLESHOOT_STATUS_URL
            )

        status = self._poll_status(kind, status_url, job, conversation_id)
        state = status.get("state", "running")

        # Still running -> either keep waiting, or give up because it is overdue.
        if state not in ("done", "failed"):
            if _is_overdue(job.get("job_started_at"), self._settings.JOB_TIMEOUT_MINUTES):
                status = {
                    "state": "failed",
                    "message": (
                        f"No result after {self._settings.JOB_TIMEOUT_MINUTES} minutes. "
                        "Your ticket stays open."
                    ),
                    "output": None,
                }
                state = "failed"
            else:
                message = status.get("message") or "Working..."
                self._conversations.update_job_message(
                    user_id, conversation_id, message
                )
                return _job_payload(
                    user_id, session_id, conversation_id, stage,
                    done=False, messages=[message],
                )

        return self._complete(
            user_id, conversation_id, session_id, stage, flow_vars, status, state
        )

    # -- internals ----------------------------------------------------------
    def sweep_running_jobs(self, limit: int = None) -> dict:
        """Advance every conversation still parked in a RUNNING stage.

        This is what the reaper timer calls. Without it a job only ever moves when a
        BROWSER polls -- so a user who closed their tab left the conversation stuck in
        DIAGNOSTICS_RUNNING forever, with a ServiceNow ticket that said "diagnostics
        started" and nothing more.

        Each conversation goes through the SAME advance_job() the FE poll uses, so there
        is one code path and one set of behaviour. If a browser and the reaper reach the
        same job together, the compare-and-swap in advance_after_job() lets exactly one
        of them win.

        One conversation failing never stops the sweep: an AppError is an expected
        problem (agent unreachable, conversation gone) and we move on; anything else is
        our bug and gets a full traceback.
        """
        limit = limit if limit is not None else self._settings.REAPER_BATCH_SIZE
        # Skip anything a browser is still polling: its updated_at is fresh, so it is
        # already being advanced by the user's own requests and does not need us.
        stale_before = (
            datetime.now(timezone.utc)
            - timedelta(minutes=self._settings.REAPER_STALE_MINUTES)
        ).isoformat()
        rows = self._conversations.find_running(limit, stale_before)
        advanced = still_running = failed = 0
        for row in rows:
            try:
                payload = self.advance_job(row["user_id"], row["id"])
                if payload.get("done"):
                    advanced += 1
                else:
                    still_running += 1
            except AppError as exc:
                logger.warning(
                    "sweep: skipped conv_id=%s: %s", row.get("id"), exc
                )
                failed += 1
            except Exception:
                logger.exception("sweep: BUG advancing conv_id=%s", row.get("id"))
                failed += 1
        return {
            "found": len(rows), "advanced": advanced,
            "still_running": still_running, "failed": failed,
        }

    def _poll_status(self, kind, status_url, job, conv_id) -> dict:
        """One poll's worth of status checking, retrying only what a retry can fix.

        ONLY UpstreamTransient is retried -- a network blip, where the next attempt may
        well succeed. If every attempt fails we report the last known message as "still
        running", so the blip costs a poll rather than the whole job.

        Everything else is deliberately NOT caught:
          * UpstreamUnavailable -- a missing URL or a contract violation. Retrying it
            three times just delays the same failure, so it surfaces immediately.
          * TypeError, KeyError, ... -- bugs in our own code. A blanket `except
            Exception` here used to swallow them and report "still running", so a real
            defect looked like a slow device job.
        """
        for _ in range(self._settings.JOB_STATUS_RETRIES):
            try:
                return self._jobs.check_status(
                    kind, status_url, job.get("job_id"),
                    job.get("job_baseline") or "",
                )
            except UpstreamTransient as exc:
                logger.warning(
                    "check_status attempt failed conv_id=%s: %s", conv_id, exc
                )
        return {
            "state": "running",
            "message": job.get("job_message") or "Working...",
            "output": None,
        }

    def _complete(
        self, user_id, conversation_id, session_id, stage, flow_vars, status, state
    ) -> dict:
        """Advance the flow after a job finishes -- exactly once, and atomically.

        The order below is deliberate:

        1. Build the reply first (the flow handlers, plus translation). These make HTTP
           calls -- to ServiceNow on the failure path, and to the multilingual agent --
           so they must run BEFORE any transaction is opened. An open transaction holds
           locks on the conversation row, and must never span a network call.

        2. Then one transaction containing exactly two writes: the conditional UPDATE
           that moves the stage and records the job outcome together, and the transcript
           rows. Either both land or neither does, so a crash can no longer leave the
           stage advanced with its messages missing.

        The conditional UPDATE is also the race guard. Only the caller that still sees
        the RUNNING stage updates a row; a second poller (two browser tabs, a refresh
        mid-poll, another worker process) gets 0 rows and skips -- which is what stops
        the same messages being written to the transcript twice.
        """
        done = state == "done"
        # What diagnostics_completed / troubleshoot_completed read off the job row. Built
        # from the status we already hold, so no write has to happen before this step.
        #
        # job_message is the USER-SAFE line; the raw device output is kept apart in
        # job_output and is never put into job_view, so it cannot reach a message list.
        job_view = {
            "job_status": JOB_DONE if done else JOB_FAILED,
            "job_message": status.get("message") or ("Complete." if done else "Failed."),
        }
        # Truncated so a pathological script can't write megabytes into the row.
        raw_output = status.get("output")
        job_output = str(raw_output)[:MAX_JOB_OUTPUT] if raw_output else None
        if job_output and not done:
            # The failure reason used to be discarded entirely. Record that we captured
            # it (not the text itself -- that would copy device data into the logs).
            logger.info(
                "job failed conv_id=%s; %d chars of device output stored in job_output",
                conversation_id, len(job_output),
            )

        messages: list = []
        if stage == DIAGNOSTICS_RUNNING:
            messages, new_stage, flow_vars = self._flow.diagnostics_completed(
                conversation_id, flow_vars, messages, job_view
            )
        else:  # TROUBLESHOOT_RUNNING
            messages, new_stage, flow_vars = self._flow.troubleshoot_completed(
                conversation_id, flow_vars, messages, job_view
            )
        messages = _visible(self._lang.translate_messages(messages, flow_vars.get("lang")))
        answer = _join(messages)

        repo = self._conversations
        with repo.transaction():
            won = repo.advance_after_job(
                user_id, conversation_id,
                expected_stage=stage, new_stage=new_stage, flow_vars=flow_vars,
                question="[job-poll]", answer=answer,
                job_status=job_view["job_status"],
                job_message=job_view["job_message"],
                job_output=job_output,
            )
            if won:
                # Job completion produces assistant-only lines (result + next question).
                # No user turn here -- the FE was polling, not chatting.
                repo.append_turns(
                    user_id, conversation_id, [("assistant", m) for m in messages]
                )

        if won:
            return _job_payload(
                user_id, session_id, conversation_id, new_stage,
                done=True, messages=messages, answer=answer,
            )

        # We lost the race. Report what the winner persisted, not our own copy, so both
        # pollers agree on what the user is looking at.
        logger.info(
            "job completion already claimed by another poller conv_id=%s", conversation_id
        )
        return self._already_advanced(user_id, conversation_id, session_id)

    def _already_advanced(self, user_id, conversation_id, session_id) -> dict:
        """Payload for a poller that lost the completion race: re-read and report."""
        conversation = self._conversations.load(user_id, conversation_id) or {}
        job = self._conversations.get_job(user_id, conversation_id)
        return _job_payload(
            user_id, session_id, conversation_id, conversation.get("stage"),
            done=True,
            messages=[job.get("job_message") or "Complete."],
        )


class HistoryService:
    """Read-only queries -- these ARE served straight from the DB."""

    def __init__(self, conversations, sessions):
        self._conversations = conversations
        self._sessions = sessions

    def list_sessions(self, user_id: str) -> dict:
        """Sidebar data: a user's most recent sessions, newest first.

        Messages are not included -- the frontend fetches those per session via
        get_transcript().
        """
        return {"sessions": self._sessions.list_recent(user_id)}

    def get_transcript(self, user_id: str, session_id: str) -> dict:
        """Return the session's messages as a flat list of {role, content}.

        `conversations` is every turn we showed the user (from conversation_turns,
        Approach A), concatenated in order -- no other conversation metadata.
        """
        conversations = []
        for row in self._conversations.list_by_session(user_id, session_id):
            conversations.extend(
                self._conversations.load_turns(user_id, row["conversation_id"])
            )
        return {
            "user_id": user_id,
            "session_id": session_id,
            "conversations": conversations,
        }


# ---------------------------------------------------------------------------
# Response shaping -- module functions because they hold no state; they just
# build the dict every return path shares.
# ---------------------------------------------------------------------------
def _chat_payload(user_id, session_id, conversation_id, stage, messages, answer) -> dict:
    """Shape the JSON a chat turn returns (identical across every return path)."""
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "stage": stage,
        "done": stage == DONE,
        "messages": messages,
        "answer": answer,
    }


def _job_payload(
    user_id, session_id, conversation_id, stage, *, done, messages, answer=None
) -> dict:
    """Shape the JSON the FE spinner expects (consistent across every return path)."""
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "stage": stage,
        "done": done,
        "messages": messages,
        "answer": answer if answer is not None else _join(messages),
    }
