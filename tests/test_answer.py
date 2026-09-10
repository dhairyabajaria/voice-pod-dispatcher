"""`dispatcherctl.sh answer` — the post and the un-park are ONE operation, or neither happens.

BOSS, 2026-09-07, on his own fourth silent failure of the night: "I act on the system through a side
channel and leave its record untouched." A QUESTION parks its item BY DESIGN so the question waits
for him. He answered four of them by direct prompt_async and never cleared the parks, so four
executors built against rows the daemon could not act on — EXEC-J finished an artifact and its
REPORT READY was skipped at 04:57 with "EXEC-J holds no dispatched, rework or reported item".

Two properties, and the second is the one that is easy to lose:
  * a refusal names what the item IS, because "refused" alone sends BOSS back to the queue file;
  * ORDER. The post happens first and the row moves ONLY if it succeeded. A row un-parked before a
    failed post says the executor is building when nobody told it anything — the same drift, in the
    other direction.

Hermetic: temp CN, stubbed roster and post. No opencode server, no live queue."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixtures                      # item 9: the shared state redirect
import fixturelog as FL

CTL = os.path.join(HERE, os.pardir, "dispatcherctl.sh")
fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)


# ---------------------------------------------------------------- ctl writes a request
def tree(status="parked", who="EXEC-J", extra=None):
    root = tempfile.mkdtemp(prefix="answer-")
    d = os.path.join(root, "test-logs", "driver")
    os.makedirs(os.path.join(d, "items")); os.makedirs(os.path.join(d, "gates"))
    it = {"id": "B.010.q", "status": status, "dispatched_to": who,
          "parked_at": "2026-09-07 04:20:00", "parked_from": "dispatched",
          "question_msg_id": "msg-77"}
    it.update(extra or {})
    json.dump({"items": [it, {"id": "B.010.other", "status": "dispatched",
                              "dispatched_to": "EXEC-B"}]},
              open(os.path.join(d, "queue.json"), "w"))
    return root, d


def ctl(root, *args):
    r = subprocess.run(["/bin/zsh", CTL, *args], capture_output=True, text=True,
                       env={**os.environ, "CN": root})
    return r.returncode, (r.stdout + r.stderr)


root, D = tree()
rc, out = ctl(root, "answer", "B.010.q", "Use the second threat model. Build.")
reqs = [f for f in os.listdir(os.path.join(D, "answerreq")) if f.endswith(".json")]
check("ctl answer writes exactly one request file", rc == 0 and len(reqs) == 1, (rc, reqs, out))
req = json.load(open(os.path.join(D, "answerreq", reqs[0])))
check("  carrying the item and the FULL text, not a truncation",
      req["item"] == "B.010.q" and req["text"] == "Use the second threat model. Build.", req)
check("  and it says the daemon posts it, not ctl", "the daemon posts it" in out, out)
check("  it also says what the row will be restored to", "will restore" in out and "dispatched" in out, out)
rc, out = ctl(root, "answer", "B.010.q", "hand-relayed", "--relayed")
req = json.load(open(os.path.join(D, "answerreq", reqs[0])))
check("--relayed is carried into the request", req["relayed_by_hand"] is True, req)
shutil.rmtree(root, ignore_errors=True)

root, D = tree(status="dispatched")
rc, out = ctl(root, "answer", "B.010.q", "text")
reqdir = os.path.join(D, "answerreq")
wrote = [f for f in os.listdir(reqdir) if f.endswith(".json")] if os.path.isdir(reqdir) else []
check("MUST-BITE  ctl refuses an item that is NOT parked and NAMES its status, writing nothing",
      rc == 1 and "dispatched" in out and not wrote, (rc, wrote, FL.flat(out)[:140]))
rc, out = ctl(root, "answer", "B.010.nope", "text")
check("an unknown id is refused by name", rc == 1 and "B.010.nope" in out, (rc, FL.flat(out)[:120]))
rc, out = ctl(root, "answer", "B.010.q")
check("no text at all is a usage error", rc == 2 and "usage" in out, (rc, FL.flat(out)[:120]))
shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- the daemon applies it
def daemon(root, d, roster=None, post=None):
    spec = importlib.util.spec_from_file_location(
        "dsp" + str(time.time_ns()), os.path.join(HERE, os.pardir, "dispatcher.py"))
    DP = importlib.util.module_from_spec(spec); spec.loader.exec_module(DP)
    fixtures.redirect_state(DP)   # item 9: never the LIVE state dir
    DP.STATE_DIR = d
    DP.ANSWER_REQ_DIR = os.path.join(d, "answerreq")
    DP.EVENTS = os.path.join(d, "events.log")
    dp = DP.Dispatcher.__new__(DP.Dispatcher)
    dp.dry = False
    dp.state = {"pending": {}, "parks": {}}
    dp.roster = roster if roster is not None else {"EXEC-J": "ses_j"}
    dp.log = lambda *a, **k: None
    dp.escalate = lambda text: escalations.append(text)
    dp.emit = lambda kind, *a: events.append((kind, a))
    dp._rm = lambda p: os.remove(p)
    # RETURNS TRUE. `posted.append(...)` returns None, and None is falsy — so until 2026-09-08 the
    # default stub modelled a post that FAILED while every check here read it as a success. It went
    # unnoticed for as long as the code ignored the result; the moment apply_answer_requests started
    # honouring post_prompt's contract, twelve green tests turned red at once. A stub that does not
    # match the real helper's contract is a test of a path the product cannot take.
    dp.post_prompt = post if post is not None else (
        lambda sid, text: (posted.append((sid, text)), True)[1])
    return DP, dp


def request(d, **kw):
    os.makedirs(os.path.join(d, "answerreq"), exist_ok=True)
    body = {"item": "B.010.q", "text": "Use the second threat model. Build.",
            "relayed_by_hand": False}
    body.update(kw)
    json.dump(body, open(os.path.join(d, "answerreq", "r.json"), "w"))


def run(root, d, q, **kw):
    global events, escalations, posted
    events, escalations, posted = [], [], []
    DP, dp = daemon(root, d, **kw)
    dp.apply_answer_requests(q)
    return dp


def loadq(d):
    return json.load(open(os.path.join(d, "queue.json")))


# (1) the happy path
root, D = tree()
q = loadq(D); request(D)
run(root, D, q)
row = q["items"][0]
check("MUST-BITE  the reply is POSTED to the executor's session",
      posted == [("ses_j", "Use the second threat model. Build.")], posted)
check("MUST-BITE  ...and the row is un-parked IN THE SAME STEP — the two cannot drift",
      row["status"] == "dispatched", row.get("status"))
check("  it is restored to what the park replaced, not guessed",
      any("parked -> dispatched" in str(a) and "restored from parked_from" in str(a)
          for k, a in events if k == "ANSWERED"), [a for k, a in events if k == "ANSWERED"])
check("  an ANSWERED event records the transition, so a closed question is visible in the log",
      [k for k, _a in events] == ["ANSWERED"], [k for k, _a in events])
check("  and the answer text is in the event, not just the fact of one",
      any("threat model" in str(a) for _k, a in events), events[:1])

# (2) a QUESTION pending row is withdrawn, or it keeps escalating at 10 and 20 minutes
root, D = tree()
q = loadq(D); request(D)
dp = run(root, D, q)
q2 = loadq(D); request(D)
events, escalations, posted = [], [], []
DP, dp = daemon(root, D)
dp.state["pending"] = {"ses_j": {"kind": "QUESTION", "item": "B.010.q", "executor": "EXEC-J"},
                       "ses_b": {"kind": "REPORT_READY", "item": "B.010.other", "executor": "EXEC-B"}}
dp.apply_answer_requests(q2)
check("MUST-BITE  the QUESTION pending row is withdrawn — otherwise it escalates at 10 and 20 min "
      "for a question that has been answered",
      "ses_j" not in dp.state["pending"], list(dp.state["pending"]))
check("CONTROL  another executor's row is untouched", "ses_b" in dp.state["pending"])

# (3) a failed post must NOT move the row
root, D = tree()
q = loadq(D); request(D)
def boom(sid, text):
    raise RuntimeError("connection refused")
run(root, D, q, post=boom)
check("MUST-BITE  a FAILED post leaves the row PARKED — a row that says building when nobody was "
      "told is the same drift in the other direction",
      q["items"][0]["status"] == "parked", q["items"][0]["status"])
check("  and it says so loudly rather than failing quiet",
      any(k == "ANSWER_FAILED" for k, _a in events) and escalations, ([k for k, _a in events], escalations[:1]))

# (4) refusals name what the item IS
for st in ("dispatched", "merged", "reported"):
    root, D = tree(status=st)
    q = loadq(D); request(D)
    run(root, D, q)
    check(f"MUST-BITE  a {st} item is refused, and the refusal NAMES {st}",
          any(k == "ANSWER_REFUSED" for k, _a in events) and any(st in str(a) for _k, a in events)
          and not posted and q["items"][0]["status"] == st,
          ([k for k, _a in events], posted))

# (5) a relay lane cannot be answered by a post that never happens
root, D = tree(who="WORKER-1")
q = loadq(D); request(D)
run(root, D, q, roster={})
check("MUST-BITE  a relay lane is REFUSED without --relayed: the daemon cannot post to it, and "
      "un-parking on a post that never happened is the bug this verb exists to prevent",
      any(k == "ANSWER_REFUSED" for k, _a in events) and not posted
      and q["items"][0]["status"] == "parked", ([k for k, _a in events], posted))
root, D = tree(who="WORKER-1")
q = loadq(D); request(D, relayed_by_hand=True)
run(root, D, q, roster={})
check("  with --relayed it un-parks on BOSS's word, and the event says no post was attempted",
      q["items"][0]["status"] == "dispatched" and not posted
      and any("no post attempted" in str(a) for k, a in events if k == "ANSWERED"),
      [a for k, a in events if k == "ANSWERED"])

# (6) a row parked before parked_from existed
root, D = tree(extra={"parked_from": None})
q = loadq(D); request(D)
run(root, D, q)
check("MUST-BITE  a row with no parked_from is restored to `dispatched` and the event SAYS the "
      "value was assumed — a guess presented as a reading is how the next drift starts",
      q["items"][0]["status"] == "dispatched"
      and any("assumed" in str(a) for k, a in events if k == "ANSWERED"),
      [a for k, a in events if k == "ANSWERED"])

# (7) the request file is consumed either way, or the next tick replays it forever
root, D = tree(status="merged")
q = loadq(D); request(D)
run(root, D, q)
check("MUST-BITE  the request file is consumed even on a refusal — otherwise every tick replays it",
      not [f for f in os.listdir(os.path.join(D, "answerreq")) if f.endswith(".json")],
      os.listdir(os.path.join(D, "answerreq")))
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- a feed hold is NOT cleared
# BOSS's ruling: a question is closed by an answer; a pause is closed by a judgement that the
# executor is fit to continue. `answer` must not resume feeding — but an un-parked row whose
# executor is still held reads as "back to work", so the event has to SAY the pause survives.
root, D = tree()
os.makedirs(os.path.join(D, "hold"), exist_ok=True)
open(os.path.join(D, "hold", "EXEC-J.feed"), "w").write("paused by circuit breaker\n")
q = loadq(D); request(D)
events, escalations, posted = [], [], []
DP, dp = daemon(root, D)
DP.HOLD_DIR = os.path.join(D, "hold")
dp.apply_answer_requests(q)
ev = [a for k, a in events if k == "ANSWERED"]
check("MUST-BITE  the ANSWERED event says a FEED HOLD is still in place — an un-parked row whose "
      "executor is still paused reads as back-to-work",
      ev and "FEED HOLD STILL IN PLACE" in str(ev[0]), ev[:1])
check("MUST-BITE  ...and the hold file is NOT removed: a verb that silently resumes feeding is a "
      "bigger surprise than one that does not",
      os.path.exists(os.path.join(D, "hold", "EXEC-J.feed")))
check("  the row is still un-parked — the question IS closed", q["items"][0]["status"] == "dispatched",
      q["items"][0]["status"])
root2, D2 = tree()
os.makedirs(os.path.join(D2, "hold"), exist_ok=True)
q2 = loadq(D2); request(D2)
events, escalations, posted = [], [], []
DP, dp = daemon(root2, D2)
DP.HOLD_DIR = os.path.join(D2, "hold")
dp.apply_answer_requests(q2)
ev2 = [a for k, a in events if k == "ANSWERED"]
check("CONTROL  no hold, no note — otherwise the warning appears on every answer and means nothing",
      ev2 and "FEED HOLD" not in str(ev2[0]), ev2[:1])
shutil.rmtree(root, ignore_errors=True); shutil.rmtree(root2, ignore_errors=True)

# ---------------------------------------------------------------- a REPORT on a PARKED row
# Three times on 2026-09-07 (EXEC-J 04:57, EXEC-H 05:10, one earlier) an executor finished an
# artifact whose row was parked, and the daemon swallowed it as an IDLE_ACK: "holds no dispatched,
# rework or reported item". That sentence is FALSE about this case — the item exists, it is parked —
# and the finished work sat with nothing pointing at it. It is a BOSS error every time it happens,
# so the daemon must RAISE it. BOSS's written checklist failed three times, which is evidence about
# the instrument rather than about his attention.
root, D = tree()
q = loadq(D)
events, escalations, posted = [], [], []
DP, dp = daemon(root, D)
dp.c = lambda k, d=None: d if k not in ("feed_queue", "auto_gate") else True
dp.current_item = lambda *a, **k: None
dp.skip_gate = lambda *a, **k: skipped.append(a) or True
skipped = []
dp.post_prompt = lambda sid, text: (posted.append((sid, text)), True)[1]
label = dp.feed(q, "EXEC-J", "ses_j", "REPORT_READY", "msg-88")
kinds = [k for k, _a in events]
check("MUST-BITE  a REPORT on a PARKED row is RAISED, not swallowed as an idle ack",
      "REPORT_ON_PARKED_ROW" in kinds and not skipped, (kinds, skipped))
check("MUST-BITE  ...and it escalates to BOSS, because it is his error every time",
      escalations and "PARKED" in escalations[0], escalations[:1])
check("  the escalation names the item and the exact command that fixes it",
      escalations and "B.010.q" in escalations[0] and "dispatcherctl.sh answer" in escalations[0],
      escalations[:1])
check("  the executor is told its work is safe and NOT to resend — the report is finished work",
      posted and "Do NOT resend" in posted[0][1] and "that is on us, not you" in posted[0][1],
      posted[:1])
check("  and no feed happens on that path", label is None, label)

# CONTROL: an executor with genuinely NO row still takes the old path, or the branch above would be
# indistinguishable from one that fires on everything.
root2, D2 = tree(who="EXEC-Z")
q2 = loadq(D2)
q2["items"][0]["dispatched_to"] = "EXEC-Z"
q2["items"][0]["status"] = "merged"
events, escalations, posted, skipped = [], [], [], []
DP, dp = daemon(root2, D2)
dp.c = lambda k, d=None: d if k not in ("feed_queue", "auto_gate") else True
dp.current_item = lambda *a, **k: None
dp.skip_gate = lambda *a, **k: skipped.append(a) or True
dp.eligible_item = lambda *a, **k: None
dp.post_prompt = lambda sid, text: (posted.append((sid, text)), True)[1]
dp.feed(q2, "EXEC-Z", "ses_z", "REPORT_READY", "msg-89")
check("CONTROL  an executor holding NOTHING still gets the ordinary skip, not the parked escalation",
      skipped and "REPORT_ON_PARKED_ROW" not in [k for k, _a in events],
      ([k for k, _a in events], skipped))
shutil.rmtree(root, ignore_errors=True); shutil.rmtree(root2, ignore_errors=True)

# ---------------------------------------------------------------- answering into a BUSY executor
# BOSS, 2026-09-07, measured from events.log:
#   10:05:19 DISPATCHED EXEC-M B.010.erasure...   10:06:03 QUESTION -> parked (correct)
#   10:06:23 DISPATCHED EXEC-M B.013.agent-editor-silent-discard (correct: the park FREED it)
#   10:08:12 ANSWERED  parked -> dispatched      => EXEC-M holding TWO dispatched rows
# The park frees the executor and the dispatcher does its job in the gap, so a restore straight back
# to `dispatched` double-books whoever answered slowly. The answer still has to reach the executor —
# a real question was really asked — but the ROW goes back to the pool pinned to that executor.
BUSY = "B.013.agent-editor-silent-discard"
root, D = tree()
q = loadq(D)
q["items"].append({"id": BUSY, "status": "dispatched", "dispatched_to": "EXEC-J"})
request(D)
run(root, D, q)
row = q["items"][0]
ev = str([a for k, a in events if k == "ANSWERED"])
check("MUST-BITE  the answer is STILL POSTED — the executor asked a real question and gets its reply",
      posted == [("ses_j", "Use the second threat model. Build.")], posted)
check("MUST-BITE  ...but the row is NOT restored to `dispatched` into an executor that has since "
      "been given other work — that is the double-booking, and it is silent",
      row["status"] == "queued", row.get("status"))
check("MUST-BITE  the row is PINNED to the same executor, which holds the context and the answer, "
      "so it is re-dispatched to EXEC-J and not handed to a stranger",
      row.get("executor") == "EXEC-J" and not row.get("dispatched_to"),
      (row.get("executor"), row.get("dispatched_to")))
check("MUST-BITE  the ANSWERED event NAMES the item that occupied the executor and says the row was "
      "queued instead — a status change nobody asked for must not be silent",
      BUSY in ev and "NOT restored to dispatched" in ev and "QUEUED and PINNED" in ev, ev[:300])
check("  EXEC-J still holds exactly ONE row afterwards — the property the whole fix is about",
      len([i for i in q["items"] if i.get("dispatched_to") == "EXEC-J"
           and i.get("status") in ("dispatched", "rework", "reported")]) == 1,
      [(i["id"], i.get("status")) for i in q["items"]])
shutil.rmtree(root, ignore_errors=True)

# CONTROL: a FREE executor still gets the ordinary restore. Without this, a fix that queued every
# answered row — losing the un-park that this whole file exists to guarantee — passes every check
# above and reads identically in the log.
root, D = tree()
q = loadq(D); request(D)
run(root, D, q)
row = q["items"][0]
check("CONTROL  a free executor is restored to `dispatched` as before, with no queued-instead note",
      row["status"] == "dispatched"
      and not any("NOT restored" in str(a) for k, a in events if k == "ANSWERED"),
      (row.get("status"), [a for k, a in events if k == "ANSWERED"]))
shutil.rmtree(root, ignore_errors=True)

# ...and a row held by a DIFFERENT executor must not trip it: B.010.other sits on EXEC-B in every
# fixture here, so if occupancy were computed board-wide instead of per-executor, the control above
# would already have been queued. Assert it directly rather than relying on that.
root, D = tree()
q = loadq(D)
q["items"].append({"id": BUSY, "status": "dispatched", "dispatched_to": "EXEC-OTHER"})
request(D)
run(root, D, q)
check("CONTROL  another executor's dispatched row is not this executor's occupancy",
      q["items"][0]["status"] == "dispatched", q["items"][0]["status"])
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- THE REAL HELPER'S FAILURE CONTRACT
# Plan 003 §4 A1 / §8. Case (3) above proves a failed post leaves the row parked — with a stub that
# RAISES. The real post_prompt never raises: every failure is caught inside it and reported as a
# RETURN VALUE of False. So the code path case (3) exercises is one the product cannot take, and the
# `except Exception` it lands in could never fire in production. Under the real helper the answer
# path called post_prompt, ignored the False, un-parked the row, withdrew the QUESTION and emitted
# ANSWERED — a delivery success recorded for a post that never happened.
#
# These cases drive the REAL method and break the transport underneath it.
def real_daemon(root, d, observing=False, boom=None, seen=None):
    """A daemon using the REAL post_prompt, with http replaced under it."""
    global events, escalations, posted
    events, escalations, posted = [], [], []
    DP, dp = daemon(root, d)
    del dp.post_prompt                     # drop the stub: the real bound method is what we test
    dp.c = lambda k, default=None: default
    dp.observing = observing
    calls = []
    def fake_http(method, path, body=None, timeout=10):
        calls.append((method, path))
        if boom:
            raise boom
        return {"ok": True}
    DP.http = fake_http
    dp.http_calls = calls
    return DP, dp

def ans(q, dp):
    dp.apply_answer_requests(q)
    return q["items"][0]

# the contract itself, stated as a check: False, not an exception
root, D = tree()
DP, dp = real_daemon(root, D, boom=ConnectionRefusedError("Connection refused"))
check("MUST-BITE  the REAL post_prompt returns False on a transport failure and raises NOTHING — "
      "this is why an `except Exception` around it can never fire",
      dp.post_prompt("ses_j", "hello") is False, "")
check("  and it records WHY, so the caller can say more than `it failed`",
      "ConnectionRefused" in getattr(dp, "_last_post_error", ""), dp._last_post_error)

# (A) a real HTTP failure beneath post_prompt: parked, pending, no false ANSWERED
root, D = tree()
q = loadq(D)
request(D)
DP, dp = real_daemon(root, D, boom=ConnectionRefusedError("Connection refused"))
dp.state["pending"] = {"ses_j": {"executor": "EXEC-J", "kind": "QUESTION", "item": "B.010.q",
                                 "msg_id": "msg-77"}}
row = ans(q, dp)
kinds = [k for k, _a in events]
check("MUST-BITE  a real HTTP failure beneath post_prompt leaves the item PARKED",
      row["status"] == "parked", row.get("status"))
check("MUST-BITE  ...its QUESTION still PENDING — the row is BOSS's only reminder that a question "
      "is open, and withdrawing it on an undelivered answer loses the question itself",
      "ses_j" in dp.state["pending"], list(dp.state["pending"]))
check("MUST-BITE  ...and NO ANSWERED event: a delivery success recorded for a post that never "
      "happened is the defect, not a cosmetic one",
      "ANSWERED" not in kinds, kinds)
check("MUST-BITE  it says so loudly, and the reason names the TRANSPORT error rather than `it "
      "failed` — the reason existed and went only to the log",
      "ANSWER_FAILED" in kinds and any("ConnectionRefused" in str(a) for k, a in events)
      and escalations, (kinds, escalations[:1]))
check("  the row keeps parked_from, so a later successful answer still restores the right status",
      row.get("parked_from") == "dispatched", row.get("parked_from"))
check("  and answered_at was NOT stamped — a timestamp is a claim that it happened",
      "answered_at" not in row, row.get("answered_at"))

# (B) OBSERVE-ONLY: the same refusal, and it must NOT read as a network fault
root, D = tree()
q = loadq(D); request(D)
DP, dp = real_daemon(root, D, observing=True)
row = ans(q, dp)
kinds = [k for k, _a in events]
check("MUST-BITE  in OBSERVE-ONLY the row also stays parked — the switch holds the actuator, so "
      "nothing was told to anyone, and un-parking would make the observe switch lose answers",
      row["status"] == "parked" and "ANSWERED" not in kinds, (row.get("status"), kinds))
check("MUST-BITE  ...and the reason says OBSERVE-ONLY, not a delivery failure: BOSS must not go "
      "hunting for a network fault that does not exist",
      any("OBSERVE-ONLY" in str(a) for k, a in events if k == "ANSWER_FAILED"),
      [a for k, a in events if k == "ANSWER_FAILED"])
check("  and no HTTP call was attempted at all", dp.http_calls == [], dp.http_calls)

# (C) CONTROL: the real helper on a WORKING transport still answers.
# Without this, a fix that refused every answer passes every check above and silently breaks the
# verb — the same failure in the other direction, which is what this file exists to prevent.
root, D = tree()
q = loadq(D); request(D)
DP, dp = real_daemon(root, D)
dp.state["pending"] = {"ses_j": {"executor": "EXEC-J", "kind": "QUESTION", "item": "B.010.q",
                                 "msg_id": "msg-77"}}
row = ans(q, dp)
kinds = [k for k, _a in events]
check("CONTROL  the REAL helper on a working transport un-parks the row and emits ANSWERED",
      row["status"] == "dispatched" and "ANSWERED" in kinds and "ANSWER_FAILED" not in kinds,
      (row.get("status"), kinds))
check("CONTROL  ...the post really was made, to the session's prompt_async endpoint",
      dp.http_calls and dp.http_calls[0][0] == "POST"
      and "/session/ses_j/prompt_async" in dp.http_calls[0][1], dp.http_calls)
check("CONTROL  ...and the QUESTION pending row is withdrawn only on that success",
      "ses_j" not in dp.state["pending"], list(dp.state["pending"]))
shutil.rmtree(root, ignore_errors=True)

# (D) the stub-shaped failure still works: a caller that DOES raise is refused too. Both roads end
# in the same refusal, so the fix is not "handle False instead of exceptions".
root, D = tree()
q = loadq(D); request(D)
def boom(sid, text):
    raise RuntimeError("connection refused")
run(root, D, q, post=boom)
check("CONTROL  an exception from a post is still a refusal — the result check did not replace the "
      "except, it joined it",
      q["items"][0]["status"] == "parked"
      and any(k == "ANSWER_FAILED" for k, _a in events), ([k for k, _a in events],))
shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- the CLASS, inventoried not fixed
# post_prompt reports failure by RETURN VALUE, so every call site that drops it records an action
# nobody performed. A1's acceptance is the answer path, and that one is fixed above. This is the
# inventory of the rest, so a NEW unchecked call site cannot be added silently while BOSS rules on
# the existing ones.
#
# THIS LIST IS NOT A CLAIM THAT THESE ARE CORRECT. It is the measured current state, awaiting a
# ruling. Pinning a defect as expected behaviour is its own failure mode, so each line says what
# happens when the post is lost, and the loudest one is named in the check itself.
import ast as _ast
_TREE = _ast.parse(open(os.path.join(HERE, os.pardir, "dispatcher.py"), errors="ignore").read())
unchecked = []
for node in _ast.walk(_TREE):
    # An Expr statement whose whole value is the call = the result is computed and thrown away.
    # Read from the AST, not from the source text: my first version grepped and matched a line of
    # PROSE inside post_prompt's own docstring, which is our own writing scoring against our own
    # detector for the fifth time.
    if not isinstance(node, _ast.Expr) or not isinstance(node.value, _ast.Call):
        continue
    fn = node.value.func
    if isinstance(fn, _ast.Attribute) and fn.attr == "post_prompt":
        unchecked.append(node.lineno)
unchecked.sort()

# The measured set, after A1. Each remaining one is a NOTE to an executor or a nudge; none of them
# moves a queue row, which is why A1's acceptance did not cover them. The loudest is the
# auto-continue at ~2511: it emits AUTO-CONTINUE and increments the counter BEFORE posting, so three
# lost posts escalate STUCK for an executor that was never prompted, with three events saying it was.
# A1b (BOSS's ruling, 2026-09-08): "a call site must honour the result if its failure changes what a
# person or the daemon later believes. A best-effort notification that moves no state and writes no
# event may drop it — and must say so in a comment."
# FIXED, because a lost post changed a belief: the auto-continue (it emitted AUTO-CONTINUE and
# incremented the counter before posting, so three lost posts escalated STUCK for an executor nobody
# had prompted); both ACK nudges (one sets the `nudged` marker that suppresses every later attempt,
# and both set a label reading `building`); the plan-review-off continue (same label problem); and
# the plan review's own feedback (the executor is stopped waiting for exactly that message).
# LEFT AS BEST-EFFORT, each with a comment at the call site saying it was decided: three courtesy
# notes to an executor whose fact is already carried by an event, an escalation or a gate row.
KNOWN_UNCHECKED = 3
check("MUST-BITE  the count of call sites that DROP post_prompt's result is the measured one — a "
      "new unchecked call site is a new instance of the A1 defect and must not appear quietly",
      len(unchecked) == KNOWN_UNCHECKED,
      f"expected {KNOWN_UNCHECKED}, found {len(unchecked)} at lines {unchecked}")
check("MUST-BITE  and the answer path is NOT among them — the fix is measured in the source, not "
      "only in the behaviour above",
      not any(1200 < n < 1240 for n in unchecked), unchecked)
SRC_LINES = open(os.path.join(HERE, os.pardir, "dispatcher.py"), errors="ignore").read().splitlines()
undocumented = [n for n in unchecked
                if not any("BEST-EFFORT BY DECISION" in ln for ln in SRC_LINES[max(0, n - 8):n])]
check("MUST-BITE  every remaining dropped result is DOCUMENTED as a decision at its own call site — "
      "otherwise the count alone cannot tell a considered best-effort from one nobody looked at",
      not undocumented, undocumented)

# --- A1b: the auto-continue counted an attempt it never sent ------------------------------------
# It emitted AUTO-CONTINUE and incremented the counter BEFORE posting. Three lost posts therefore
# walked the counter to max and escalated STUCK for an executor that was never prompted, with three
# log lines saying it had been — the board reporting what it SENT rather than what happened.
root, D = tree()
DP, dp = daemon(root, D)
dp.state = {"pending": {}, "parks": {}, "auto": {}, "handled": {}, "nudged": {}}
dp.c = lambda k, default=None: {"max_auto_continue": 3}.get(k, default)
events.clear()
sent = []
dp.post_prompt = lambda sid, text: (sent.append(text), False)[1]   # the transport is down
dp._last_post_error = "ConnectionRefusedError: Connection refused"
labels = {}
q = loadq(D)
# HONEST LABEL: the loop below is a MODEL of the branch, not the branch — the real one sits deep in
# tick() behind a roster, a queue lock and three classifications. So the property it demonstrates is
# pinned twice: here as behaviour, and below as the ORDER OF THE REAL STATEMENTS in the source. The
# source check is the one that would survive me rewriting this model to match a broken fix.
for _ in range(3):
    # drive the same branch three times, exactly as three ticks would
    n = int(dp.state["auto"].get("ses_j", 0))
    if n < 3:
        if dp.post_prompt("ses_j", "CONTINUE"):
            dp.state["auto"]["ses_j"] = n + 1
            dp.emit("AUTO-CONTINUE", "EXEC-J", "ses_j", "m1", f"{n + 1}/3", "")
        else:
            seen = dp.state.setdefault("auto_failed", {})
            if seen.get("ses_j") != "m1":
                seen["ses_j"] = "m1"
                dp.emit("AUTO_CONTINUE_FAILED", "EXEC-J", "ses_j", "m1", f"attempt {n + 1}/3 NOT sent", "x")
check("MUST-BITE  three FAILED auto-continues consume NO attempts — the counter is a record of what "
      "was sent, and a counter that walks to max on lost posts escalates STUCK for an executor "
      "nobody prompted",
      dp.state["auto"].get("ses_j", 0) == 0, dp.state["auto"])
check("MUST-BITE  ...and no AUTO-CONTINUE event claims a prompt that never left",
      not any(k == "AUTO-CONTINUE" for k, _a in events), [k for k, _a in events])
check("  the failure is announced ONCE per message, not once per 5-second tick",
      [k for k, _a in events].count("AUTO_CONTINUE_FAILED") == 1, [k for k, _a in events])
# CONTROL: a DELIVERED continue still counts, or the fix is "never auto-continue" wearing a new name.
dp.state["auto"] = {}
events.clear()
dp.post_prompt = lambda sid, text: True
n = int(dp.state["auto"].get("ses_j", 0))
if dp.post_prompt("ses_j", "CONTINUE"):
    dp.state["auto"]["ses_j"] = n + 1
    dp.emit("AUTO-CONTINUE", "EXEC-J", "ses_j", "m1", f"{n + 1}/3", "")
check("CONTROL  a DELIVERED auto-continue still counts and still emits — the fix must not be "
      "`never auto-continue` under a new name",
      dp.state["auto"]["ses_j"] == 1 and any(k == "AUTO-CONTINUE" for k, _a in events),
      (dp.state["auto"], [k for k, _a in events]))
# and the source really has the post BEFORE the record, which is the whole property
SRC = "\n".join(SRC_LINES)
i = SRC.find("if self.post_prompt(sid, CONTINUE_PROMPT.format(")
check("MUST-BITE  in the SOURCE the post comes first and the counter follows it — a test that only "
      "drove a copy of the branch would pass over the original order",
      i != -1 and SRC.find('self.state["auto"][sid] = n + 1', i) > i
      and SRC.find('self.emit("AUTO-CONTINUE"', i) > i, i)

print(("\nFAILED: " + ", ".join(fails)) if fails else "\nall green")
sys.exit(1 if fails else 0)
