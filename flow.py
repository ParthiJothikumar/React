"""Support-flow engine: the stage machine, as a class over its injected clients.

SupportFlow.step() advances a conversation by one turn and returns
(messages, new_stage, flow_vars). It holds no per-conversation state -- the caller owns
`stage` and `flow_vars` and passes them in every turn -- so a single instance is safe to
share across requests and the handlers stay pure functions of their arguments.

What it holds instead is its collaborators: the agent, ServiceNow and multilingual
clients, injected in __init__. That is the reason this is a class rather than a module
of functions. Previously the handlers imported `run_agent_json`, `snow_create_incident`
and friends directly at module scope, which meant the only way to test a stage was to
monkeypatch module globals. Now a test constructs SupportFlow(fake_agents, fake_snow,
fake_multilingual) and drives any branch with no network at all.

Still framework-agnostic: no FastAPI objects, no DB access, no HTTP status codes. It
raises ConversationEnded (see errors.py); routes.py decides that means 409.
"""
import json

from app.config import Settings, logger
from app.constants import (
    ACTION_AUTOMATIC,
    ACTION_MANUAL,
    AWAITING_CLASSIFY,
    AWAITING_FINAL,
    AWAITING_FOLLOWUP,
    AWAITING_ISSUE,
    AWAITING_PROCEED,
    AWAITING_RESOLVED,
    DIAGNOSTICS_RUNNING,
    DONE,
    GREETING_PROMPT,
    ISSUE_GREETING,
    ISSUE_NON_IT,
    ISSUE_NON_OUTLOOK_IT,
    JOB_FAILED,
    JOB_KIND_DIAGNOSTIC,
    JOB_KIND_TROUBLESHOOT,
    NON_IT_PROMPT,
    NONACTIONABLE_END_MESSAGE,
    RUNNING_STAGES,
    SECOND_CLASS_FALLBACK,
    TROUBLESHOOT_RUNNING,
    VALID_ISSUE_TYPES,
)
from app.errors import ConversationEnded


# NOTE ON DUPLICATE TICKETS -- why there is no correlation_id here.
#
# A key derived from user_id + the message (stable across a retry) was built and then
# removed on purpose. It only pays off if ServiceNow matches OPEN incidents only:
#
#   same complaint while the old ticket is open   -> reuse is reasonable
#   same complaint after the old one was closed   -> MUST create a new ticket
#
# Get that second rule wrong and a user's new problem is attached to a closed ticket
# nobody is working -- their issue silently disappears. A wrong merge is worse than the
# duplicate it prevents, so the field is not sent until ServiceNow can guarantee the
# scoping.
#
# What replaces it: routes.py no longer tells a user to "try again" once a ticket exists
# -- it hands them the incident number and says they need not start over. That removes
# the retry we were causing, which was the main source of duplicates. The cases still
# uncovered are a user ignoring that message, and double-submits (two tabs / double
# click) -- the latter produces duplicates even when nothing fails, and only a
# ServiceNow-side key could catch it.


class SupportFlow:
    """The stage machine. One instance per worker process; no mutable state."""

    def __init__(self, agents, servicenow, multilingual, settings: Settings):
        self._agents = agents
        self._snow = servicenow
        self._lang = multilingual
        self._settings = settings

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------
    def step(self, conv_id: str, user_message: str, stage, flow_vars: dict):
        """Advance the support flow by one turn based on the current stage.

        Detects and stores the user's language, then dispatches to the handler for the
        current stage. Returns (messages, new_stage, flow_vars). Raises
        ConversationEnded if the stage is already terminal (unknown/DONE) -- routes.py
        maps that to a 409.
        """
        messages: list = []
        # Re-detect every turn so a mid-conversation language switch is honored; keep
        # the previously stored language when detection is empty/uncertain/unsupported.
        flow_vars["lang"] = self._lang.detect(
            user_message, fallback=flow_vars.get("lang", "")
        )

        # Work internally in English: translate the inbound message so the yes/no
        # checks AND the sub-agents all see English, regardless of the user's language.
        # (The services still store the ORIGINAL native text.)
        user_message = self._lang.to_english(user_message, flow_vars["lang"])

        if stage in RUNNING_STAGES:
            # A job is running -> the FE should be polling /jobs/status, not sending
            # chat turns. If a turn arrives anyway, say we're still working and STAY on
            # the stage we were given (don't try to re-derive which job is running).
            messages.append("Still working on it -- I'll update you here shortly.")
            new_stage, flow_vars = stage, flow_vars
        else:
            handler = self._handler_for(stage)
            if handler is None:
                raise ConversationEnded("Conversation already completed")
            messages, new_stage, flow_vars = handler(
                conv_id, user_message, flow_vars, messages
            )

        # The interaction represents the chat contact -> close it once the conversation
        # ends (any terminal), whether or not an incident was created/resolved.
        if (
            new_stage == DONE
            and flow_vars.get("interaction_id")
            and not flow_vars.get("interaction_closed")
        ):
            self._snow.close_interaction(conv_id, flow_vars["interaction_id"])
            flow_vars["interaction_closed"] = True
        return messages, new_stage, flow_vars

    def _handler_for(self, stage):
        """Map a stage to its handler method, or None if the stage is terminal.

        A dict beats a long if/elif chain here: adding a stage is one entry, and the
        set of handled stages is readable at a glance. RUNNING stages are handled in
        step() itself, because their reply keeps whatever stage came in.
        """
        if stage is None:
            return self._start
        return {
            AWAITING_ISSUE: self._start,
            AWAITING_CLASSIFY: self._run_classify,
            AWAITING_RESOLVED: self._resolved_turn,
            AWAITING_PROCEED: self._proceed_turn,
            AWAITING_FINAL: self._final_turn,
            AWAITING_FOLLOWUP: self._followup_turn,
        }.get(stage)

    # -----------------------------------------------------------------------
    # Stage handlers
    # -----------------------------------------------------------------------
    def _start(self, conv_id, user_message, flow_vars, messages):
        """First turn / greeting loop: open the interaction, classify, and route.

        The ServiceNow interaction is opened ONCE, at the start of the conversation
        (any category), and reused on greeting-loop turns. Routing by issue_type:
          greeting / non_it -> re-prompt for an Outlook issue up to MAX_NONACTIONABLE
                               times (shared counter), then end. No incident.
          non_outlook_it    -> create an incident (ticket), thank the user, end.
          outlook_it        -> create an incident up front, then enter classification.
        The interaction is closed centrally in step() when the conversation ends.
        """
        # Interaction is created once per conversation, at the start (reused on loops).
        if not flow_vars.get("interaction_id"):
            flow_vars["interaction_id"] = self._snow.create_interaction(
                conv_id, flow_vars.get("user_id", "")
            )
        interaction_id = flow_vars["interaction_id"]

        response = self._agents.call_json(
            conv_id, self._settings.ORCHESTRATOR_AGENT, user_message
        )
        # Validate issue_type against the allowed set (no value drift): a missing or
        # unexpected label is logged and treated as non_it, so it can never silently
        # fall through to the incident-creating branches below.
        issue_type = response.get("issue_type")
        if issue_type not in VALID_ISSUE_TYPES:
            logger.warning(
                "orchestrator returned unexpected issue_type=%r conv_id=%s; treating as %s",
                issue_type, conv_id, ISSUE_NON_IT,
            )
            issue_type = ISSUE_NON_IT
        flow_vars["issue_type"] = issue_type

        # Non-actionable (greeting or general/non-IT): re-prompt up to the cap, then end.
        if issue_type in (ISSUE_GREETING, ISSUE_NON_IT):
            count = flow_vars.get("nonactionable_count", 0) + 1
            flow_vars["nonactionable_count"] = count
            if count > self._settings.MAX_NONACTIONABLE:
                messages.append(NONACTIONABLE_END_MESSAGE)
                return messages, DONE, flow_vars
            messages.append(
                GREETING_PROMPT if issue_type == ISSUE_GREETING else NON_IT_PROMPT
            )
            return messages, AWAITING_ISSUE, flow_vars

        # non_outlook_it: create the ticket under the interaction, thank the user, end.
        if issue_type == ISSUE_NON_OUTLOOK_IT:
            snow = self._snow.create_incident(
                conv_id, interaction_id, issue_type, user_message
            )
            flow_vars["incident_id"] = snow.get("incident_id")
            ticket_msg = (
                snow.get("message")
                or f"Your ticket {snow.get('incident_id')} has been created."
            )
            messages.append(ticket_msg + " Thank you for contacting us.")
            return messages, DONE, flow_vars

        # outlook_it: create the incident up front, then enter the classification flow.
        snow = self._snow.create_incident(
            conv_id, interaction_id, issue_type, user_message
        )
        flow_vars["incident_id"] = snow.get("incident_id")
        messages.append(
            snow.get("message") or f"Incident {snow.get('incident_id')} has been created."
        )
        return self._run_classify(conv_id, user_message, flow_vars, messages)

    def _run_classify(self, conv_id, user_message, flow_vars, messages):
        """First-classification turn: run the FIRST classification agent.

        While it needs more info it returns chat_close=false with a follow-up question
        (agent_message); we show it and stay in AWAITING_CLASSIFY. When it returns
        chat_close=true we store its kb_id + summary and hand off to the second
        classification agent.
        """
        first = self._agents.call_json(
            conv_id, self._settings.FIRST_CLASSIFICATION_AGENT, user_message
        )

        if not first.get("chat_close"):
            # still gathering info -> show the follow-up question and wait
            if first.get("agent_message"):
                messages.append(first["agent_message"])
            return messages, AWAITING_CLASSIFY, flow_vars

        # classification complete -> keep kb_id + summary for the second agent
        flow_vars["kb_id"] = first.get("kb_id")
        flow_vars["kb_summary"] = first.get("summary")
        if first.get("agent_message"):
            messages.append(first["agent_message"])

        # First call to the SECOND classification agent (message=""), then route.
        second = self._agents.call_second_classification(
            conv_id, flow_vars.get("kb_id"), message=""
        )
        return self._route_second_classification(conv_id, flow_vars, messages, second)

    def _followup_turn(self, conv_id, user_message, flow_vars, messages):
        """User answered an ask_user follow-up -> re-call agent 2 with their reply.

        The agent owns the follow-up count (we don't send or track it); it returns
        another follow-up question, a ready-to-run result, or success=false when it
        gives up. The response is routed exactly like the first call.
        """
        second = self._agents.call_second_classification(
            conv_id, flow_vars.get("kb_id"), message=user_message
        )
        return self._route_second_classification(conv_id, flow_vars, messages, second)

    def _route_second_classification(self, conv_id, flow_vars, messages, second):
        """Route on one ValidationResponse from the SECOND classification agent.

        `second` is the agent's decision dict (the M/A/U/N scenarios). Order matters:
        success and validated are checked first, so every failure or non-automatable
        result (M2/U5/U6/N1/N2) short-circuits to _close_with_message before any
        action / user_params / follup_flag branch is considered.
        """
        # success == false (M2/U5/U6/N2, or a synthesized transport failure) -> close.
        if not second.get("success"):
            return self._close_with_message(
                conv_id, flow_vars, messages, second, log_error=True
            )

        # success but not automatable (N1: e.g. not found) -> show message, close.
        if not second.get("validated"):
            return self._close_with_message(
                conv_id, flow_vars, messages, second, log_error=False
            )

        action = (second.get("action") or "").lower()

        # MANUAL (M1): steps already embed the reference link -> show + ask confirmation.
        if action == ACTION_MANUAL:
            messages.append(
                second.get("steps") or second.get("agent_message") or ""
            )
            messages.append("Did these steps resolve your issue?")
            return messages, AWAITING_RESOLVED, flow_vars

        # Drift guard: anything but a clean "automatic" -> route to the team (never
        # auto-run).
        if action != ACTION_AUTOMATIC:
            logger.warning(
                "second classification returned unexpected action=%r conv_id=%s; "
                "routing to support team", second.get("action"), conv_id,
            )
            return self._close_with_message(
                conv_id, flow_vars, messages, second, log_error=False
            )

        # AUTOMATIC, no user input needed (A1) -> diagnostics, then troubleshoot.
        if not second.get("user_params"):
            return self._start_diagnostics(conv_id, flow_vars, messages)

        # AUTOMATIC + ask_user, still gathering input (U1/U2/U4) -> ask and wait. The
        # agent owns the follow-up count and returns success=false at the cap (U5).
        if second.get("follup_flag"):
            if second.get("follow_up_question"):
                messages.append(second["follow_up_question"])
            return messages, AWAITING_FOLLOWUP, flow_vars

        # AUTOMATIC + ask_user, inputs ready (U3) -> troubleshoot with the script.
        return self._start_troubleshoot(
            conv_id, flow_vars, messages,
            second.get("script_name"), second.get("script_params"),
        )

    def _close_with_message(self, conv_id, flow_vars, messages, second, log_error):
        """Route-to-team tail (validated==false / success==false).

        The user only ever sees a safe message: agent_message when present (keeps M2's
        KB link and U5's 'team will follow up'), else SECOND_CLASS_FALLBACK. The raw
        technical `error` is logged and stored in flow_vars["last_error"] (persisted on
        the conversation row) for debugging -- never shown to the user. The incident,
        opened at the start, is updated and left OPEN for the support team.
        """
        raw_error = second.get("error")
        if log_error and raw_error:
            logger.error(
                "second-classification failed conv_id=%s stop=%s: %s",
                conv_id, second.get("stop_reason"), raw_error,
            )
            flow_vars["last_error"] = raw_error
        messages.append(second.get("agent_message") or SECOND_CLASS_FALLBACK)
        self._snow.update_incident(
            conv_id, flow_vars.get("incident_id"),
            "Automated resolution not completed; routed to support team.",
        )
        return messages, DONE, flow_vars

    # -----------------------------------------------------------------------
    # Async job triggers
    # -----------------------------------------------------------------------
    def _start_diagnostics(self, conv_id, flow_vars, messages):
        """A1 path: START an async diagnostics job instead of blocking.

        We only record the intent in flow_vars["start_job"]; the service (after step())
        calls the agent's /start and records the job_id on the conversation row. The FE
        then polls /jobs/status, which checks the agent's /status live and advances the
        flow. Diagnostics feeds into troubleshoot afterwards (AWAITING_PROCEED).
        """
        self._snow.update_incident(
            conv_id, flow_vars.get("incident_id"), "Automated diagnostics started."
        )
        flow_vars["start_job"] = {
            "kind": JOB_KIND_DIAGNOSTIC,
            "start_url": self._settings.DIAGNOSTICS_START_URL,
            "status_url": self._settings.DIAGNOSTICS_STATUS_URL,
            "input": json.dumps(
                {
                    "kb_id": flow_vars.get("kb_id"),
                    "summary": flow_vars.get("kb_summary", ""),
                }
            ),
        }
        messages.append(
            "Diagnostics started -- this can take a few minutes. I'll post updates here."
        )
        return messages, DIAGNOSTICS_RUNNING, flow_vars

    def _start_troubleshoot(self, conv_id, flow_vars, messages, script_name, script_params):
        """U3 path: skip diagnostics and START the troubleshoot job with the collected
        script. Same start_job mechanism as _start_diagnostics; the job input carries
        the script_name + script_params the ask_user loop gathered.
        """
        self._snow.update_incident(
            conv_id, flow_vars.get("incident_id"), "Troubleshooting started."
        )
        flow_vars["start_job"] = {
            "kind": JOB_KIND_TROUBLESHOOT,
            "start_url": self._settings.TROUBLESHOOT_START_URL,
            "status_url": self._settings.TROUBLESHOOT_STATUS_URL,
            "input": json.dumps(
                {
                    "kb_id": flow_vars.get("kb_id"),
                    "script_name": script_name,
                    "script_params": script_params or {},
                }
            ),
        }
        messages.append(
            "Troubleshooting started -- this can take a few minutes. I'll post updates here."
        )
        return messages, TROUBLESHOOT_RUNNING, flow_vars

    # -----------------------------------------------------------------------
    # Confirmation turns
    # -----------------------------------------------------------------------
    def _resolved_turn(self, conv_id, user_message, flow_vars, messages):
        """Handle the 'did the manual steps resolve it?' answer (manual path).

        'yes' resolves the incident opened at the start. 'no' updates that incident
        (it stays open for the team). Flow ends (DONE) either way.
        """
        if "yes" in user_message.lower():
            messages.append(
                self._snow.resolve(
                    conv_id,
                    flow_vars.get("interaction_id"),
                    flow_vars.get("incident_id"),
                )
            )
            messages.append("Glad it worked")
            return messages, DONE, flow_vars
        # user said no -> update the incident, which stays open
        self._snow.update_incident(
            conv_id, flow_vars.get("incident_id"),
            "Manual steps did not resolve the issue.",
        )
        messages.append(
            f"Your incident {flow_vars.get('incident_id', '')} stays open for the "
            "support team."
        )
        return messages, DONE, flow_vars

    def _proceed_turn(self, conv_id, user_message, flow_vars, messages):
        """Handle the 'proceed with troubleshooting?' answer.

        'yes' STARTS an async troubleshoot job (same start_job mechanism -- we never
        block); anything else leaves the incident open and ends (DONE).
        """
        if "yes" in user_message.lower():
            self._snow.update_incident(
                conv_id, flow_vars.get("incident_id"), "Troubleshooting started."
            )
            flow_vars["job_kind"] = JOB_KIND_TROUBLESHOOT
            flow_vars["start_job"] = {
                "kind": JOB_KIND_TROUBLESHOOT,
                "start_url": self._settings.TROUBLESHOOT_START_URL,
                "status_url": self._settings.TROUBLESHOOT_STATUS_URL,
                "input": flow_vars.get("kb_summary", ""),
            }
            messages.append(
                "Troubleshooting started -- this can take a few minutes. "
                "I'll post updates here."
            )
            return messages, TROUBLESHOOT_RUNNING, flow_vars
        # user said no -> update the incident, which stays open
        self._snow.update_incident(
            conv_id, flow_vars.get("incident_id"), "User declined troubleshooting."
        )
        messages.append(
            f"No problem. Your incident {flow_vars.get('incident_id', '')} stays open "
            "for the support team."
        )
        return messages, DONE, flow_vars

    def _final_turn(self, conv_id, user_message, flow_vars, messages):
        """Handle the final 'issue resolved?' answer after troubleshooting.

        'yes' resolves the ServiceNow incident; anything else leaves it open. Flow
        ends (DONE) either way.
        """
        if "yes" in user_message.lower():
            messages.append(
                self._snow.resolve(
                    conv_id,
                    flow_vars.get("interaction_id"),
                    flow_vars.get("incident_id"),
                )
            )
            return messages, DONE, flow_vars
        # user said no -> update the incident, which stays open
        self._snow.update_incident(
            conv_id, flow_vars.get("incident_id"),
            "Issue not resolved after troubleshooting.",
        )
        messages.append(
            f"Your incident {flow_vars.get('incident_id', '')} stays open for the "
            "support team."
        )
        return messages, DONE, flow_vars

    # -----------------------------------------------------------------------
    # Job completion (called from JobService, not from step())
    # -----------------------------------------------------------------------
    def diagnostics_completed(self, conv_id, flow_vars, messages, job):
        """Advance the flow after a diagnostics job finishes.

        On success: show the result and ask whether to proceed to troubleshooting
        (AWAITING_PROCEED). On failure/timeout: note it on the incident and end (DONE).
        """
        if job.get("job_status") == JOB_FAILED:
            self._snow.update_incident(
                conv_id, flow_vars.get("incident_id"), "Automated diagnostics failed."
            )
            messages.append(
                f"Diagnostics couldn't complete ({job.get('job_message', '')}). Your "
                f"incident {flow_vars.get('incident_id', '')} stays open for the "
                "support team."
            )
            return messages, DONE, flow_vars
        # job_message is the user-safe outcome line. The raw device output lives in
        # job_output and is never handed to this method -- see JobsClient.
        if job.get("job_message"):
            messages.append(job["job_message"])
        messages.append("Proceed with troubleshooting?")
        return messages, AWAITING_PROCEED, flow_vars

    def troubleshoot_completed(self, conv_id, flow_vars, messages, job):
        """Advance the flow after a troubleshoot job finishes.

        On success: show the result and ask whether the issue is resolved
        (AWAITING_FINAL). On failure/timeout: note it on the incident and end (DONE).
        """
        if job.get("job_status") == JOB_FAILED:
            self._snow.update_incident(
                conv_id, flow_vars.get("incident_id"), "Troubleshooting failed."
            )
            messages.append(
                f"Troubleshooting couldn't complete ({job.get('job_message', '')}). "
                f"Your incident {flow_vars.get('incident_id', '')} stays open for the "
                "support team."
            )
            return messages, DONE, flow_vars
        # job_message is the user-safe outcome line (see diagnostics_completed).
        if job.get("job_message"):
            messages.append(job["job_message"])
        messages.append("Issue Resolved?")
        return messages, AWAITING_FINAL, flow_vars
