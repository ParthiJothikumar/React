"""Domain constants -- the vocabulary of the support flow.

These are NOT configuration: they are fixed parts of the domain (stage names, the
labels the agents are allowed to return, the fixed prompts). They never come from the
environment, so they stay plain module-level constants rather than moving onto the
Settings class in config.py. Anything read from an env var / App Setting belongs
there instead.
"""

# ---------------------------------------------------------------------------
# Flow stages
# ---------------------------------------------------------------------------
AWAITING_ISSUE = "AWAITING_ISSUE"
AWAITING_CLASSIFY = "AWAITING_CLASSIFY"
AWAITING_RESOLVED = "AWAITING_RESOLVED"
AWAITING_PROCEED = "AWAITING_PROCEED"
AWAITING_FINAL = "AWAITING_FINAL"
# ask_user follow-up turn (chat-await, NOT a RUNNING stage): the second
# classification agent asked a question and we're waiting for the user's reply.
AWAITING_FOLLOWUP = "AWAITING_FOLLOWUP"
DONE = "DONE"

# Diagnostics/Troubleshoot run as long-running ASYNC jobs (3-15 min, Intune on the
# client machine). The flow does NOT block: it starts the job and enters one of these
# "running" stages; the FE polls GET /jobs/status, which checks the agent's /status
# live and advances the flow when the job finishes.
DIAGNOSTICS_RUNNING = "DIAGNOSTICS_RUNNING"
TROUBLESHOOT_RUNNING = "TROUBLESHOOT_RUNNING"
RUNNING_STAGES = frozenset({DIAGNOSTICS_RUNNING, TROUBLESHOOT_RUNNING})

# ---------------------------------------------------------------------------
# Agent response vocabularies (drift guards)
# ---------------------------------------------------------------------------
# Allowed issue_type values returned by the ORCHESTRATOR agent. Anything outside
# this set is classification drift: it is logged and handled as non_it (re-prompt)
# so an unexpected label can never silently create an incident.
ISSUE_GREETING = "greeting"
ISSUE_NON_IT = "non_it"
ISSUE_NON_OUTLOOK_IT = "non_outlook_it"
ISSUE_OUTLOOK_IT = "outlook_it"
VALID_ISSUE_TYPES = frozenset(
    {ISSUE_GREETING, ISSUE_NON_IT, ISSUE_NON_OUTLOOK_IT, ISSUE_OUTLOOK_IT}
)

# Allowed mode values returned by the SECOND classification agent. An unexpected
# value is logged and handled as MODE_MANUAL (show steps, ask the user) so a drift
# can never silently trigger automated diagnostics.
MODE_MANUAL = "manual"
MODE_AUTOMATE = "automate"
VALID_MODES = frozenset({MODE_MANUAL, MODE_AUTOMATE})

# action values returned by the SECOND classification agent's richer contract
# (ValidationResponse). Note the automate value is "automatic" here (not "automate").
ACTION_MANUAL = "manual"
ACTION_AUTOMATIC = "automatic"

# ---------------------------------------------------------------------------
# Fixed user-facing lines
# ---------------------------------------------------------------------------
# Single user-safe line shown on a failure that carries no agent_message. The raw
# technical `error` is stored in flow_vars["last_error"] / logged, never shown.
SECOND_CLASS_FALLBACK = (
    "Sorry, we couldn't complete this automatically. A support team member "
    "will follow up to help."
)

# Non-actionable messages (greeting or general/non-IT): the orchestrator re-prompts
# for an Outlook issue up to Settings.MAX_NONACTIONABLE times (shared counter), then
# ends the conversation. No incident is created for these; the interaction (opened at
# the start of every conversation) is closed when the conversation ends.
GREETING_PROMPT = "Hi! How can I help you with your Outlook issue?"
NON_IT_PROMPT = "This is Outlook IT support -- what Outlook issue can I help you with?"
NONACTIONABLE_END_MESSAGE = (
    "Thanks for contacting. For any Outlook support, please start a new conversation."
)

# ---------------------------------------------------------------------------
# Async jobs
# ---------------------------------------------------------------------------
JOB_KIND_DIAGNOSTIC = "diagnostic"
JOB_KIND_TROUBLESHOOT = "troubleshoot"

# Values written to conversations.job_status.
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_TERMINAL = frozenset({JOB_DONE, JOB_FAILED})
