#####################################################################################
## Project name : Dummy ServiceNow Agent (local testing stub)                       #
## Business owner , Team : Data and AIA                                             #
## Purpose of Function:                                                             #
##   HTTP-triggered Azure Function that MOCKS the ServiceNow ticketing agent so the  #
##   orchestrator can be tested end-to-end WITHOUT a real ServiceNow instance.      #
##   It parses the orchestrator's "action=... | key=value" message and returns fake #
##   interaction / incident ids in the exact fields the orchestrator reads.         #
##                                                                                  #
##   Actions handled (must match the orchestrator's snow_* helpers):                #
##     action=create_interaction | user=<user_id>                                   #
##         -> {"interaction_id": "IMS0001234", "message": "..."}                    #
##     action=close_interaction  | interaction_id=<id>                              #
##         -> {"message": "..."}                                                    #
##     action=create_incident    | interaction_id=<id> | issue_type=<..> |          #
##                                 details=<..>                                     #
##         -> {"incident_id": "INC0009876", "message": "..."}                       #
##     action=update_incident    | incident_id=<id> | note=<..>                     #
##         -> {"incident_id": "INC0009876", "message": "..."}                       #
##     action=resolve            | interaction_id=<id> | incident_id=<id>           #
##         -> {"message": "..."}                                                    #
#####################################################################################

# # Load all the libraries
import json
import logging
import random

import azure.functions as func


#=============================Logging Setup========================================================
logging.getLogger().setLevel(logging.INFO)


#==============================================Utilities===========================================
def json_response(payload: dict, status: int = 200) -> func.HttpResponse:
    """Return a JSON HTTP response (ensure_ascii=False keeps any non-Latin text readable)."""
    return func.HttpResponse(
        body=json.dumps(payload, ensure_ascii=False),
        status_code=status,
        mimetype="application/json",
    )


def extract_message(req: func.HttpRequest) -> str:
    """Read the orchestrator's 'message' (the action string) from body or query.

    The orchestrator POSTs {"conversation_id": ..., "message": "action=... | ..."}.
    We also accept ?message=... for quick manual curl testing.
    """
    try:
        body = req.get_json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        return str(body.get("message"))
    return req.params.get("message", "") or ""


def parse_action(message: str) -> dict:
    """Parse 'action=create_incident | interaction_id=IMS1 | details=...' into a dict.

    Splits on '|' then on the FIRST '=' so a value may itself contain '='. NOTE: if a
    free-text value (e.g. details) contains a literal '|', it will be truncated at that
    pipe -- fine for a dummy, but a real agent should use a JSON body to avoid this.
    """
    fields: dict = {}
    for part in (message or "").split("|"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def fake_number(prefix: str) -> str:
    """Fake ServiceNow-style record number, e.g. INC0012345 / IMS0007654."""
    return f"{prefix}{random.randint(0, 9999999):07d}"


#==============================================Azure Function Entry================================
def main(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP entry point: parse the action and return a mocked ServiceNow result."""
    message = extract_message(req)
    fields = parse_action(message)
    action = fields.get("action", "")
    logging.info("Dummy ServiceNow: action=%s fields=%s", action, fields)

    try:
        if action == "create_interaction":
            interaction_id = fake_number("IMS")
            return json_response(
                {
                    "interaction_id": interaction_id,
                    "user": fields.get("user", ""),
                    "message": f"Interaction {interaction_id} created.",
                }
            )

        if action == "close_interaction":
            interaction_id = fields.get("interaction_id", "")
            return json_response(
                {
                    "interaction_id": interaction_id,
                    "message": f"Interaction {interaction_id} closed.",
                }
            )

        if action == "create_incident":
            incident_id = fake_number("INC")
            interaction_id = fields.get("interaction_id", "")
            under = f" under interaction {interaction_id}" if interaction_id else ""
            return json_response(
                {
                    "incident_id": incident_id,
                    "interaction_id": interaction_id,
                    "issue_type": fields.get("issue_type", ""),
                    "message": f"Incident {incident_id} has been created{under}.",
                }
            )

        if action == "update_incident":
            incident_id = fields.get("incident_id", "")
            return json_response(
                {
                    "incident_id": incident_id,
                    "note": fields.get("note", ""),
                    "message": f"Incident {incident_id} updated.",
                }
            )

        if action == "resolve":
            incident_id = fields.get("incident_id", "")
            return json_response(
                {
                    "incident_id": incident_id,
                    "interaction_id": fields.get("interaction_id", ""),
                    "message": f"Incident {incident_id} has been resolved.",
                }
            )

        # Unknown / empty action -> harmless ack so the orchestrator never breaks.
        return json_response(
            {"message": "ServiceNow (dummy): no matching action.", "action": action}
        )

    except Exception as exc:  # final safety net
        logging.exception("Dummy ServiceNow agent error")
        return json_response(
            {"message": "Dummy ServiceNow agent error.", "details": str(exc)},
            status=500,
        )
