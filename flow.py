"""Support-flow engine (stage machine + per-stage handlers).

Framework-agnostic business logic: step() advances the conversation by one turn and
returns (messages, new_stage, flow_vars). It talks only to the config constants and
the client modules -- no FastAPI request/response objects, no DB access -- so it can
be unit-tested in isolation and reused from a non-HTTP entry point later.
"""
import json

from fastapi import HTTPException

from app.clients.agents import (
    call_second_classification,
    run_agent,
    run_agent_json,
)
from app.clients.multilingual import detect_language, translate_to_english
from app.clients.servicenow import (
    snow_close_interaction,
    snow_create_incident,
    snow_create_interaction,
    snow_update_incident,
)
from app.config import (
    AWAITING_CLASSIFY,
    AWAITING_FINAL,
    AWAITING_ISSUE,
    AWAITING_PROCEED,
    AWAITING_RESOLVED,
    DIAGNOSTICS_AGENT,
    DONE,
    FIRST_CLASSIFICATION_AGENT,
    GREETING_PROMPT,
    ISSUE_GREETING,
    ISSUE_NON_IT,
    ISSUE_NON_OUTLOOK_IT,
    MAX_NONACTIONABLE,
    MODE_AUTOMATE,
    MODE_MANUAL,
    NON_IT_PROMPT,
    NONACTIONABLE_END_MESSAGE,
    ORCHESTRATOR_AGENT,
    SERVICENOW_AGENT,
    TROUBLESHOOT_AGENT,
    VALID_ISSUE_TYPES,
    VALID_MODES,
    logger,
)


def step(conv_id: str, user_message: str, stage, flow_vars: dict):
    """Advance the support flow by one turn based on the current stage.

    Detects and stores the user's language, then dispatches to the handler for the
    current stage. Returns (messages, new_stage, flow_vars). Raises 409 if the stage
    is already terminal (unknown/DONE).
    """
    messages: list[str] = []
    # Re-detect every turn so a mid-conversation language switch is honored; keep
    # the previously stored language when detection is empty/uncertain/unsupported.
    flow_vars["lang"] = detect_language(
        user_message, fallback=flow_vars.get("lang", "")
    )

    # Work internally in English: translate the inbound message so the yes/no
    # checks AND the sub-agents all see English, regardless of the user's language.
    # (The endpoints still store request.message -- the original native text.)
    user_message = translate_to_english(user_message, flow_vars["lang"])

    if stage is None or stage == AWAITING_ISSUE:
        result = _start(conv_id, user_message, flow_vars, messages)
    elif stage == AWAITING_CLASSIFY:
        result = _run_classify(conv_id, user_message, flow_vars, messages)
    elif stage == AWAITING_RESOLVED:
        result = _resolved_turn(conv_id, user_message, flow_vars, messages)
    elif stage == AWAITING_PROCEED:
        result = _proceed_turn(conv_id, user_message, flow_vars, messages)
    elif stage == AWAITING_FINAL:
        result = _final_turn(conv_id, user_message, flow_vars, messages)
    else:
        raise HTTPException(status_code=409, detail="Conversation already completed")

    messages, new_stage, flow_vars = result
    # The interaction represents the chat contact -> close it once the conversation
    # ends (any terminal), whether or not an incident was created/resolved.
    if (
        new_stage == DONE
        and flow_vars.get("interaction_id")
        and not flow_vars.get("interaction_closed")
    ):
        snow_close_interaction(conv_id, flow_vars["interaction_id"])
        flow_vars["interaction_closed"] = True
    return messages, new_stage, flow_vars


def _start(conv_id, user_message, flow_vars, messages):
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
        flow_vars["interaction_id"] = snow_create_interaction(
            conv_id, flow_vars.get("user_id", "")
        )
    interaction_id = flow_vars["interaction_id"]

    response = run_agent_json(conv_id, ORCHESTRATOR_AGENT, user_message)
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
        if count > MAX_NONACTIONABLE:
            messages.append(NONACTIONABLE_END_MESSAGE)
            return messages, DONE, flow_vars
        messages.append(
            GREETING_PROMPT if issue_type == ISSUE_GREETING else NON_IT_PROMPT
        )
        return messages, AWAITING_ISSUE, flow_vars

    # non_outlook_it: create the ticket under the interaction, thank the user, end.
    if issue_type == ISSUE_NON_OUTLOOK_IT:
        snow = snow_create_incident(conv_id, interaction_id, issue_type, user_message)
        flow_vars["incident_id"] = snow.get("incident_id")
        ticket_msg = (
            snow.get("message")
            or f"Your ticket {snow.get('incident_id')} has been created."
        )
        messages.append(ticket_msg + " Thank you for contacting us.")
        return messages, DONE, flow_vars

    # outlook_it: create the incident up front, then enter the classification flow.
    snow = snow_create_incident(conv_id, interaction_id, issue_type, user_message)
    flow_vars["incident_id"] = snow.get("incident_id")
    messages.append(
        snow.get("message") or f"Incident {snow.get('incident_id')} has been created."
    )
    return _run_classify(conv_id, user_message, flow_vars, messages)


def _run_classify(conv_id, user_message, flow_vars, messages):
    """First-classification turn: run the FIRST classification agent.

    While it needs more info it returns chat_close=false with a follow-up question
    (agent_message); we show it and stay in AWAITING_CLASSIFY. When it returns
    chat_close=true we store its kb_id + summary and hand off to the second
    classification agent.
    """
    first_classification = run_agent_json(
        conv_id, FIRST_CLASSIFICATION_AGENT, user_message
    )

    if not first_classification.get("chat_close"):
        # still gathering info -> show the follow-up question and wait
        if first_classification.get("agent_message"):
            messages.append(first_classification["agent_message"])
        return messages, AWAITING_CLASSIFY, flow_vars

    # classification complete -> keep kb_id + summary for the second agent
    flow_vars["kb_id"] = first_classification.get("kb_id")
    flow_vars["kb_summary"] = first_classification.get("summary")
    if first_classification.get("agent_message"):
        messages.append(first_classification["agent_message"])

    return _run_second_classification(conv_id, flow_vars, messages)


def _run_second_classification(conv_id, flow_vars, messages):
    """Second-classification decision: manual steps vs automated diagnostics.

    Sends the first agent's summary + kb_id to the SECOND classification agent.
    'manual' shows the steps and asks whether they resolved the issue
    (AWAITING_RESOLVED). Otherwise ('automate') it updates the incident (opened at
    the start), runs diagnostics, and asks to proceed (AWAITING_PROCEED).
    """
    second_classification = call_second_classification(
        conv_id, flow_vars.get("kb_summary", ""), flow_vars.get("kb_id")
    )
    # Validate mode against the allowed set (no value drift): a missing or
    # unexpected mode is logged and handled as MODE_MANUAL, so a drift can never
    # silently trigger the automated-diagnostics branch below.
    mode = (second_classification.get("mode") or "").lower()
    if mode not in VALID_MODES:
        logger.warning(
            "second classification returned unexpected mode=%r conv_id=%s; "
            "defaulting to %s",
            mode, conv_id, MODE_MANUAL,
        )
        mode = MODE_MANUAL
    flow_vars["mode"] = mode

    if mode == MODE_MANUAL:
        if second_classification.get("steps"):
            messages.append(second_classification["steps"])
        if second_classification.get("agent_message"):
            messages.append(second_classification["agent_message"])
        messages.append("Did these steps resolve your issue?")
        return messages, AWAITING_RESOLVED, flow_vars

    # automate: the incident was opened at the start -> UPDATE it (reached diagnostics)
    details = flow_vars.get("kb_summary", "")
    snow_update_incident(
        conv_id, flow_vars.get("incident_id"), "Automated diagnostics started."
    )
    messages.append("Diagnosis Flow started")
    messages.append(
        run_agent(
            conv_id,
            DIAGNOSTICS_AGENT,
            json.dumps({"kb_id": flow_vars.get("kb_id"), "summary": details}),
        )
    )
    messages.append("Proceed with troubleshooting?")
    return messages, AWAITING_PROCEED, flow_vars


def _resolved_turn(conv_id, user_message, flow_vars, messages):
    """Handle the 'did the manual steps resolve it?' answer (manual path).

    'yes' resolves the incident opened at the start. 'no' updates that incident
    (it stays open for the team). Flow ends (DONE) either way.
    """
    if "yes" in user_message.lower():
        messages.append(
            run_agent(
                conv_id,
                SERVICENOW_AGENT,
                f"action=resolve | interaction_id={flow_vars.get('interaction_id', '')} | "
                f"incident_id={flow_vars.get('incident_id', '')}",
            )
        )
        messages.append("Glad it worked")
        return messages, DONE, flow_vars
    # user said no -> update the incident, which stays open
    snow_update_incident(
        conv_id, flow_vars.get("incident_id"), "Manual steps did not resolve the issue."
    )
    messages.append(
        f"Your incident {flow_vars.get('incident_id', '')} stays open for the support team."
    )
    return messages, DONE, flow_vars


def _proceed_turn(conv_id, user_message, flow_vars, messages):
    """Handle the 'proceed with troubleshooting?' answer.

    'yes' runs the Troubleshoot agent and asks whether the issue is resolved
    (AWAITING_FINAL); anything else falls back to ServiceNow and ends (DONE).
    """
    if "yes" in user_message.lower():
        # reached troubleshoot -> update the incident, then run troubleshoot
        snow_update_incident(conv_id, flow_vars.get("incident_id"), "Troubleshooting started.")
        messages.append("Troubleshoot started")
        messages.append(
            run_agent(conv_id, TROUBLESHOOT_AGENT, flow_vars.get("kb_summary", ""))
        )
        messages.append("Issue Resolved?")
        return messages, AWAITING_FINAL, flow_vars
    # user said no -> update the incident, which stays open
    snow_update_incident(conv_id, flow_vars.get("incident_id"), "User declined troubleshooting.")
    messages.append(
        f"No problem. Your incident {flow_vars.get('incident_id', '')} stays open for the "
        "support team."
    )
    return messages, DONE, flow_vars


def _final_turn(conv_id, user_message, flow_vars, messages):
    """Handle the final 'issue resolved?' answer after troubleshooting.

    'yes' resolves the ServiceNow incident; anything else falls back to ServiceNow
    (e.g. escalate/keep open). Flow ends (DONE) either way.
    """
    if "yes" in user_message.lower():
        messages.append(
            run_agent(
                conv_id,
                SERVICENOW_AGENT,
                f"action=resolve | interaction_id={flow_vars.get('interaction_id', '')} | "
                f"incident_id={flow_vars.get('incident_id', '')}",
            )
        )
        return messages, DONE, flow_vars
    # user said no -> update the incident, which stays open
    snow_update_incident(
        conv_id, flow_vars.get("incident_id"), "Issue not resolved after troubleshooting."
    )
    messages.append(
        f"Your incident {flow_vars.get('incident_id', '')} stays open for the support team."
    )
    return messages, DONE, flow_vars
