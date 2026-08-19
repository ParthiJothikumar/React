"""Timer trigger: finish off diagnostic/troubleshoot jobs nobody is watching.

WHY THIS EXISTS
A job only ever moved forward when the FRONTEND polled GET /jobs/status. So if the user
closed their tab -- or slept the laptop, or lost signal -- the conversation stayed in
DIAGNOSTICS_RUNNING permanently: the stage never advanced, the ServiceNow ticket kept
saying "Automated diagnostics started" and nothing more, and it sat in their sidebar as
an active chat that did nothing. The old give-up rule couldn't help, because it counted
POLLS, and a poll only happens while a browser is open.

WHAT IT DOES
Every minute it asks for conversations still parked in a RUNNING stage and puts each one
through JobService.advance_job() -- the exact method the FE poll calls. So a sweep and a
poll are indistinguishable to the rest of the system: same status check, same
compare-and-swap, same transaction, same tests.

Mostly it finds nothing. If the user is watching, their own poll advances the job first
and this sweep is a no-op. It only matters for the conversations that were abandoned.

WHY A TIMER TRIGGER RATHER THAN A THREAD
The Functions host runs this on its own clock, which gives two things an in-process loop
cannot: it runs ONCE across every instance (the host takes a lease), and it WAKES the app
if it has scaled to zero. A background thread would also die whenever the worker is
recycled, and there is no FastAPI lifespan hook here to start one from.

Each run is a short pass, not a wait -- it takes one look and exits, so it is nowhere near
the host's functionTimeout. REAPER_BATCH_SIZE caps how much one sweep will do; a backlog
simply drains over the following sweeps.

It does NOT notify anyone. It makes the state correct; telling the user their diagnostic
finished while they were away is a separate feature (email / Teams / SignalR).
"""
import azure.functions as func

from app.config import logger
from app.deps import database, make_job_service


def main(timer: func.TimerRequest) -> None:
    """One sweep. Never raises -- a timer that throws just retries the same failure."""
    if timer.past_due:
        logger.warning("reaper is running late")

    conn = database.connect()
    try:
        result = make_job_service(conn).sweep_running_jobs()
        # Logged at info even when empty: a silent reaper and a dead reaper look the
        # same otherwise, and this is the line that proves it is alive.
        logger.info(
            "reaper sweep: found=%s advanced=%s still_running=%s failed=%s",
            result["found"], result["advanced"],
            result["still_running"], result["failed"],
        )
    except Exception:
        logger.exception("reaper sweep failed")
    finally:
        conn.close()
