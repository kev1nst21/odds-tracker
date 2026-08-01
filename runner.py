"""Polls the market repeatedly inside one CI run.

Why this file exists: GitHub Actions refuses to schedule a workflow more often
than every 5 minutes, and even that it runs late routinely -- measured in
production on 2026-07-30, a "*/30" schedule fired 4 times in 19 hours instead
of 38. A cron of "*/3" simply does not work there.

So the workflow starts a run and this runner does its own timed loop inside
the window. That is the only way to get a real 3-minute cadence out of GitHub,
and it has a useful side effect: the polls are spaced by a real clock rather
than by whenever the runner happened to boot, so the intervals in the
experiment are actually the intervals we claim they are.

Three things it is careful about:

  * Poll times are anchored to absolute wall-clock multiples of the interval,
    not to "now + interval". Otherwise every cycle drifts by however long the
    previous poll took (~60s), and by the end of the day the 3-minute bucket is
    really a 4-minute one -- which would quietly invalidate the comparison the
    whole experiment exists to make.
  * It stops before the next run is due, so two runs never overlap and a
    late-starting run shortens itself instead of being killed mid-write. A
    killed run loses its database cache, and with it the track record.
  * It watches the provider's credit balance. A 3-minute cadence burns quota
    ten times faster than a 30-minute one; running the account to zero would
    take the whole product offline, which is far worse than a few missed polls.
"""
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

from config import (
    POLL_INTERVAL_MINUTES,
    PUBLISH_INTERVAL_MINUTES,
    CADENCE_LABEL,
    QUOTA_RESERVE_CREDITS,
)
import main
import odds_client

# Leave the tail of the window free so the workflow still has time to publish
# the dashboard before the next run starts.
TAIL_MARGIN_SECONDS = 150


def _window_end(now: datetime) -> datetime:
    """When this run must stop polling, so the dashboard still gets published
    before the next run is due.

    Measured from OUR OWN start, not from the clock's half-hour boundaries.
    The first version anchored to :00/:30, which broke the moment the cron
    moved off those minutes: a run starting at :07 would quit at :27 and leave
    a ten-minute hole every cycle. Start-relative also means a run GitHub
    launched late still gets its full window instead of a stub.
    """
    return now + timedelta(minutes=max(1, PUBLISH_INTERVAL_MINUTES)) \
               - timedelta(seconds=TAIL_MARGIN_SECONDS)


def _poll_times(now: datetime, end: datetime, interval_min: int):
    """Absolute times to poll at: every `interval_min` minutes past the hour,
    starting with the first such moment at or after `now`."""
    step = timedelta(minutes=interval_min)
    anchor = now.replace(minute=0, second=0, microsecond=0)
    t = anchor
    while t < now:
        t += step
    while t < end:
        yield t
        t += step


def _quota_exhausted() -> bool:
    remaining = odds_client.LAST_QUOTA.get("remaining")
    if remaining is None:
        return False
    if remaining <= QUOTA_RESERVE_CREDITS:
        print(f"[runner] stopping: only {remaining} credits left "
              f"(reserve is {QUOTA_RESERVE_CREDITS}); polling resumes once the "
              f"plan rolls over")
        return True
    return False


def run():
    started = datetime.now(timezone.utc)
    end = _window_end(started)
    interval = max(1, POLL_INTERVAL_MINUTES)

    print(f"[runner] cadence: every {interval} min"
          + (f" ({CADENCE_LABEL})" if CADENCE_LABEL else "")
          + f"; this run polls until {end:%H:%M:%S} UTC")

    polls = ok = 0
    # Poll immediately on start, then follow the clock. Without the immediate
    # first poll a run that starts at :01 with a 30-minute cadence would sit
    # idle for 29 minutes and publish nothing.
    schedule = [started] + [t for t in _poll_times(started, end, interval)
                            if t > started + timedelta(seconds=30)]

    for target in schedule:
        wait = (target - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        if datetime.now(timezone.utc) >= end:
            break

        polls += 1
        try:
            main.run_once()
            ok += 1
        except Exception:  # noqa: BLE001 -- one bad poll must not end the run
            print(f"[runner] poll {polls} failed:")
            traceback.print_exc()

        if _quota_exhausted():
            break

    print(f"[runner] done: {ok}/{polls} polls succeeded in this window")
    # A run where every single poll failed is a real failure and should show up
    # red in CI rather than passing quietly.
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
