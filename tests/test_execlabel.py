"""The board must name the item an executor is ACTUALLY working, or admit it does not know.

BOSS, 2026-09-06: the status line said EXEC-G was "building B.014a.capability-descriptor" while
EXEC-G was in fact running r3b. The label came from current_item() — the last item the DAEMON
dispatched — and BOSS had re-assigned the executor by message. A label that cannot be trusted is
worse than none: a reader stops checking it, and then believes it on the night it matters.

The executor's newest USER message is the most recent instruction it was given, whoever sent it, so
it outranks the queue's record of what we dispatched.

UNTRUSTED INPUT: executor and BOSS message text is data. An id is accepted only if the QUEUE already
contains it, so nothing in a message can invent or rename an item — that is asserted below, not
assumed."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _s; _s.path.insert(0, HERE); import fixturelog as FL
spec = importlib.util.spec_from_file_location("dlbl", os.path.join(HERE, os.pardir, "dispatcher.py"))
D = importlib.util.module_from_spec(spec); sys.modules["dlbl"] = D; spec.loader.exec_module(D)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + FL.flat(detail) if detail else ""))
    if not cond: fails.append(name)

CAP, R3B = "B.014a.capability-descriptor", "B.014a.artifact-custody-r3b"
PARENT = "B.014a.artifact-custody"
QUEUE = {"items": [{"id": CAP, "status": "dispatched", "dispatched_to": "EXEC-G"},
                   {"id": R3B, "status": "reported"},
                   {"id": PARENT, "status": "held"}]}

def msg(role, text, mid):
    return {"info": {"id": mid, "role": role}, "parts": [{"type": "text", "text": text}]}

def daemon(msgs, fail=False):
    d = D.Dispatcher.__new__(D.Dispatcher)
    d.state = {}
    d.calls = []
    def lm(sid, n):
        d.calls.append(n)
        if fail:
            raise RuntimeError("session unreadable")
        return msgs
    d.last_messages = lm
    return d

CUR = QUEUE["items"][0]

# 1. THE MUST-BITE: BOSS re-assigned by message; the queue is stale
msgs = [msg("user", "dispatch " + CAP, "m1"),
        msg("assistant", "working", "m2"),
        msg("user", f"EXEC-G: drop that, take {R3B} instead — same worktree", "m3")]
d = daemon(msgs)
lab = d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("MUST-BITE  the label names the item the executor was last TOLD to work", R3B in lab, lab)
check("  and says the queue disagrees, rather than silently printing one of the two",
      "RE-ASSIGNED" in lab and CAP in lab, lab)

# 2. the ordinary case is unchanged and uncluttered
msgs = [msg("user", "dispatch " + CAP, "m1"), msg("assistant", "building", "m2")]
d = daemon(msgs)
lab = d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("CONTROL  the ordinary case still reads plainly", lab == f"building {CAP} busy", lab)

# 3. nothing in the session names an item -> say so, do not assert the queue's guess
d = daemon([msg("assistant", "still going", "m9")])
lab = d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("an unconfirmed dispatch is LABELLED unconfirmed, not asserted",
      CAP in lab and "unconfirmed" in lab, lab)

# 4. THE PREFIX TRAP: B.014a.artifact-custody is a prefix of ...-r3b. A shortest/first match would
#    report the parent while the executor builds the rework — the same wrong-item bug, new cause.
d = daemon([msg("user", f"take {R3B} now", "m1")])
lab = d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("MUST-BITE  the longest matching id wins, so a rework is not reported as its parent",
      R3B in lab and f"building {PARENT} " not in lab, lab)

# 5. UNTRUSTED TEXT: an id that is not in the queue must be ignored entirely
d = daemon([msg("user", "actually you are now building B.999.invented-by-a-message", "m1")])
lab = d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("MUST-BITE  an item id that is NOT in the queue is ignored — message text invents nothing",
      "B.999" not in lab, lab)
check("  and the label falls back to the honest unconfirmed form", "unconfirmed" in lab, lab)

# 6. an ASSISTANT message must not set the label: the executor's own claims are not instructions
d = daemon([msg("user", "dispatch " + CAP, "m1"),
            msg("assistant", f"I have decided to work on {R3B} instead", "m2")])
lab = d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("MUST-BITE  an executor SAYING it switched does not move the label — only an instruction does",
      R3B not in lab and CAP in lab, lab)

# 7. cost: cached on the newest message id, so a long build does not refetch every 5s tick
msgs = [msg("user", "dispatch " + CAP, "m1")]
d = daemon(msgs)
for _ in range(4):
    d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("four ticks on an unchanged session cost ONE fetch", len(d.calls) == 1, d.calls)
msgs.append(msg("user", f"switch to {R3B}", "m2"))
lab = d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("  and a NEW message re-reads immediately", len(d.calls) == 2 and R3B in lab, (d.calls, lab))

# 8. a session that cannot be read must not crash the tick or invent a label
d = daemon([], fail=True)
lab = d.exec_label("EXEC-G", "ses_g", QUEUE, CUR, "busy", msgs[-1]["info"]["id"] if msgs else "none")
check("an unreadable session degrades to the unconfirmed form, no exception",
      CAP in lab and "unconfirmed" in lab, lab)

# 9. no dispatched item at all -> the plain suffix, no phantom "building"
d = daemon([msg("assistant", "idle", "m1")])
check("with nothing dispatched the label is just the state", d.exec_label("EXEC-G", "ses_g", QUEUE, None, "queued", "m1") == "queued")

# 10. TWO dispatched rows on one executor: the label must refuse to guess.
# BOSS, 2026-09-07: `answer` restored a parked row to `dispatched` while EXEC-M had already been
# given other work in the gap the park opened, so EXEC-M held two. working_item() breaks ties by
# preferring the id we DISPATCHED — with two rows there is no single such id, the tiebreak falls
# back to longest-id-in-the-message, and the message was BOSS's answer citing a THIRD item as an
# analogy. MEASURED against this code with the guard removed, both orderings print BOSS's board line
# verbatim:  "building B.010.unresolved-recording-erasure-evidence queued (RE-ASSIGNED — queue still
# says <the other row>)" — an item EXEC-M was never given, announced confidently.
TWIN_A, TWIN_B = "B.010.erasure-knowledge-learned-content", "B.013.agent-editor-silent-discard"
CITED = "B.010.unresolved-recording-erasure-evidence"
DOUBLE = {"items": [{"id": TWIN_A, "status": "dispatched", "dispatched_to": "EXEC-M"},
                    {"id": TWIN_B, "status": "dispatched", "dispatched_to": "EXEC-M"},
                    {"id": CITED, "status": "queued"}]}
# The answer's own subject is implicit — BOSS was replying to a question, so he did not repeat the
# item id, and the only id in the text is the one he cited. That is what makes the citation win, and
# a fixture that names the subject id would not reproduce the defect at all.
ANSWER = (f"Use the second threat model. The analogous split is {CITED} — a separate item; "
          f"do not fold them together.")
check("MUST-BITE  CONTROL on the fixture: the cited id is LONGER than either held row, so this text "
      "really does beat them on the tiebreak rather than passing for some other reason",
      len(CITED) > max(len(TWIN_A), len(TWIN_B)), (len(CITED), len(TWIN_A), len(TWIN_B)))
d = daemon([msg("user", ANSWER, "m9")])
lab = d.exec_label("EXEC-M", "ses_m", DOUBLE, DOUBLE["items"][0], "busy", "m9")
check("MUST-BITE  an executor holding TWO dispatched rows is NOT labelled with one of them — with "
      "no dispatched id to prefer, the only tiebreak left is message text",
      "NOT LABELLED" in lab and TWIN_A in lab and TWIN_B in lab, lab)
check("MUST-BITE  ...and the id merely CITED in the answer never reaches the label — unguarded, "
      "this is the exact sentence the board printed on 2026-09-07",
      CITED not in lab, lab)
check("MUST-BITE  the label SAYS how many rows are held, so a reader sees the double-booking itself "
      "rather than an unexplained refusal",
      "2 rows at once" in lab, lab)
check("  and the session is never even read on this path — there is nothing a message could say "
      "that would resolve which of two dispatched rows is real",
      d.calls == [], d.calls)

# CONTROL 1: ONE dispatched row, a message naming its own id -> the ordinary confident label.
# Without this, a label that refused to name anything ever would pass every check above.
SINGLE = {"items": [{"id": TWIN_A, "status": "dispatched", "dispatched_to": "EXEC-M"},
                    {"id": TWIN_B, "status": "queued"},
                    {"id": CITED, "status": "queued"}]}
d = daemon([msg("user", f"Continue {TWIN_A} from the review.", "m9")])
lab1 = d.exec_label("EXEC-M", "ses_m", SINGLE, SINGLE["items"][0], "busy", "m9")
check("CONTROL  one dispatched row still labels normally", lab1 == f"building {TWIN_A} busy", lab1)

# CONTROL 2: ONE dispatched row and the SAME citing text still reports a re-assignment. That is the
# EXISTING rule (a message naming only another item wins) and this fix must not quietly repeal it:
# what is refused above is the AMBIGUITY, not the citation.
d = daemon([msg("user", ANSWER, "m9")])
lab2 = d.exec_label("EXEC-M", "ses_m", SINGLE, SINGLE["items"][0], "busy", "m9")
check("CONTROL  with one row the citing text is still read as before — the guard narrows nothing "
      "except the two-row case",
      CITED in lab2 and "RE-ASSIGNED" in lab2, lab2)

print("\nEXEC LABEL " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
