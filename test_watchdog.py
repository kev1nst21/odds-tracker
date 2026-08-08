"""Offline check of the watchdog, replaying the 2026-08-06 outage.

The bug it guards against is subtle and the guard itself is dangerous if wrong
-- a watchdog that cancels healthy runs is worse than no watchdog. So both
directions are asserted: it must fire on the real jam, and it must stay
completely silent while the queue is merely busy.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["WATCHDOG_STATE"] = os.path.join(tempfile.mkdtemp(), "wd.json")
os.environ["GH_TOKEN"] = "x"

import watchdog  # noqa: E402

now = datetime.now(timezone.utc)


def iso(minutes_ago):
    return (now - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def run(n, status, conclusion=None, age=10):
    return {"id": 1000 + n, "run_number": n, "status": status,
            "conclusion": conclusion, "created_at": iso(age), "updated_at": iso(age)}


cancelled, dispatched, sent = [], [], []
watchdog._cancel = lambda w: cancelled.append(w["run_number"]) or True
watchdog._dispatch = lambda: dispatched.append(1) or True
watchdog._telegram = lambda t: sent.append(t)
watchdog._save_state = lambda s: watchdog.__dict__.setdefault("_mem", {}).update(s)
watchdog._state = lambda: watchdog.__dict__.get("_mem", {})


# --- the actual incident ----------------------------------------------------
# #695 waiting for 46 hours, nothing executing, last success two days back.
watchdog._runs = lambda limit=40: [
    run(710, "queued", age=120),
    run(695, "waiting", age=46 * 60),
    run(694, "completed", "success", age=46 * 60 + 1),
]
watchdog.main()
assert 695 in cancelled and 710 in cancelled, cancelled
assert dispatched, "queue cleared but no restart requested"
assert sent and "опрос встал" in sent[0], sent
assert "46.0 ч" in sent[0] or "46" in sent[0], sent[0]
print(f"watchdog ok: сняло {sorted(cancelled)}, перезапустило, написало в бот")

# the SAME state an hour later must not produce a second message
sent.clear(); cancelled.clear()
watchdog.main()
assert not sent, "incident re-announced on the next hour"
print("watchdog ok: об инциденте пишет один раз, а не каждый час")

# --- recovery ---------------------------------------------------------------
sent.clear(); cancelled.clear()
watchdog._runs = lambda limit=40: [run(712, "completed", "success", age=3)]
watchdog.main()
assert not cancelled, cancelled
assert sent and "снова идёт" in sent[0], sent
print("watchdog ok: о восстановлении сообщает и снимает флаг")

# --- a busy but healthy queue must be left ALONE ----------------------------
# One run executing, its successor waiting behind it for 45 minutes. This is
# poll.yml working exactly as designed; touching it would break the chain.
sent.clear(); cancelled.clear(); dispatched.clear()
watchdog._runs = lambda limit=40: [
    run(714, "queued", age=45),
    run(713, "in_progress", age=50),
    run(712, "completed", "success", age=55),
]
watchdog.main()
assert not cancelled, f"cancelled a healthy queued run: {cancelled}"
assert not dispatched, "restarted a poller that was already running"
assert not sent, sent
print("watchdog ok: пока прогон идёт, очередь не трогает вообще")

# --- a young queued run is never touched either -----------------------------
cancelled.clear()
watchdog._runs = lambda limit=40: [
    run(715, "queued", age=5),
    run(712, "completed", "success", age=20),
]
watchdog.main()
assert not cancelled, cancelled
print("watchdog ok: свежий прогон в очереди — это норма, не трогает")
