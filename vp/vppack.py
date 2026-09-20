#!/usr/bin/env python3
"""vppack.py -- the v13 packet layer (PACKET-FORMAT-v13 §1/§6/§6b, PACK-COMPLETE §4.3).

A packet (03-PACKETS/<ID>/PACKET.md + BENCHMARK.md) is the unit the Architect
writes; a scheduler task (run-state.json) is the unit orchestration_control.py
dispatches.  This module binds the two, deterministically and without a model:

  load_pack(dir)                 -> {id: packet}, [lint lines]
  bind(packet, tasks)            -> ("direct", X) | ("dynamic", T, template) | ("hold", why)
  dependency_tasks(...)          -> the task ids a dynamic packet's instantiate depends on
  parameters_for(...)            -> the template's required parameters, from the header
  closure_plan(packet, pack, tasks) -> which `closes` targets retire now, which wait (§6b)

Binding rule (D9): `scheduler_task: X` drives X directly while X has not
finished (PLANNED/WAITING/READY/CLAIMED/RUNNING); once X is finished or stuck
(VERIFIED/INTEGRATED/REPAIR_REQUIRED/BLOCKED/INVALID_EVIDENCE/CANCELLED) the
packet runs as a dynamic task -- id = packet id, or <X>-V13 when they coincide
-- from `template`, or from the v13_kind when the template is `none`.
`NEW:<template>` is always dynamic with id = packet id.  stdlib only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import vplint
except ImportError:  # pragma: no cover
    vplint = None

UNFINISHED = ("PLANNED", "WAITING_DEPENDENCY", "READY", "CLAIMED", "DISPATCHED", "RUNNING", "RESULT_RECEIVED")
ACCEPTED = ("VERIFIED", "INTEGRATED")
KIND_TEMPLATE = {"regrade": "EVIDENCE_RECOVERY", "probe": "EVIDENCE_RECOVERY", "repair": "REPAIR",
                 "integrate": "REPAIR", "chain": None, "review": None}
REVIEW_TEMPLATES = ("JUNIOR_REVIEW", "SECURITY_REVIEW", "FINAL_REFRESH")
HEADER_LISTS = ("depends_on", "releases", "owned_files", "forbidden_files", "test_paths", "closes")
DISPATCH_RECORD = "DISPATCH.json"      # written into <worktree>/.vp/ for every packet turn


def _front_matter(text):
    if vplint is not None:
        fm = vplint.parse_front_matter(text)
        return fm[0] if isinstance(fm, tuple) else fm
    # minimal fallback: key: value / key: [a, b] / - item lists
    fm, key = {}, None
    body = text.split("---", 2)[1] if text.startswith("---") else ""
    for line in body.splitlines():
        if line.startswith("  - ") and key:
            fm.setdefault(key, []).append(line[4:].strip())
        elif ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            val = val.split("#", 1)[0].strip()
            key = key.strip()
            if val.startswith("[") and val.endswith("]"):
                fm[key] = [v.strip() for v in val[1:-1].split(",") if v.strip()]
            elif val == "":
                fm[key] = []
            else:
                fm[key] = val
    return fm


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            return [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        return [v] if v else []
    return list(v)


def _as_bool(v):
    return str(v).strip().lower() in ("true", "yes", "1")


def load_pack(pack_dir):
    """every <dir>/PACKET.md with a front matter -> packet dict; lint lines for
    dangling depends_on, id/dir mismatch, and unknown scheduler_task forms."""
    pack_dir = Path(pack_dir)
    pack, lint = {}, []
    for d in sorted(p for p in pack_dir.iterdir() if p.is_dir()):
        pm = d / "PACKET.md"
        if not pm.exists():
            continue
        text = pm.read_text(encoding="utf-8")
        fm = _front_matter(text)
        pid = str(fm.get("item") or d.name)
        if pid != d.name:
            lint.append("ERROR pack: %s/PACKET.md item %r != directory" % (d.name, pid))
        st = str(fm.get("scheduler_task") or pid)
        packet = {
            "id": pid, "dir": str(d), "title": str(fm.get("title") or ""),
            "v13_kind": str(fm.get("v13_kind") or "regrade"), "scheduler_task": st,
            "template": None if str(fm.get("template") or "none") == "none" else str(fm.get("template")),
            "runner_role": str(fm.get("runner_role") or ""), "closes": _as_list(fm.get("closes")),
            "depends_on": _as_list(fm.get("depends_on")), "hosted_owed": _as_bool(fm.get("hosted_owed")),
            "critical": _as_bool(fm.get("critical")), "owner_gate": str(fm.get("owner_gate") or "none"),
            "base_sha": str(fm.get("base_sha") or ""), "owned_files": _as_list(fm.get("owned_files")),
            "test_paths": _as_list(fm.get("test_paths")), "proof_kind": str(fm.get("proof_kind") or "platform"),
            "max_rounds": int(fm.get("max_rounds") or 4), "parent_contract": fm.get("parent_contract"),
            "group": fm.get("group"), "body": text,
            # D17/D21 review subject keys (the driver's _review_packet_plan)
            "review_base": str(fm.get("review_base") or "").strip(),
            "coverage_targets": [str(t).strip() for t in _as_list(fm.get("coverage_targets"))],
        }
        if st.startswith("NEW:"):
            tpl = st[4:]
            if packet["template"] and packet["template"] != tpl:
                lint.append("ERROR pack: %s scheduler_task %s vs template %s" % (pid, st, packet["template"]))
            packet["template"] = packet["template"] or tpl
        pack[pid] = packet
    for pid, p in pack.items():
        for dep in p["depends_on"]:
            if dep not in pack:
                lint.append("ERROR pack: %s depends_on %s is not a packet" % (pid, dep))
    for pid in sorted(pack):
        for twin in hosted_twins(pack[pid], Path(pack[pid]["dir"]) / "BENCHMARK.md", lint):
            pack.setdefault(twin["id"], twin)
    return pack, lint


# -- binding ------------------------------------------------------------------------------

def dynamic_id(packet):
    st = packet["scheduler_task"]
    if st.startswith("NEW:") or st != packet["id"]:
        return packet["id"]
    return "%s-V13" % packet["id"]


def template_for(packet):
    if packet["template"]:
        return packet["template"]
    if packet["v13_kind"] == "review":
        return {"security": "SECURITY_REVIEW", "final": "FINAL_REFRESH"}.get(packet["runner_role"], "JUNIOR_REVIEW")
    return KIND_TEMPLATE.get(packet["v13_kind"])


def bind(packet, tasks):
    """-> ("direct", task_id) | ("dynamic", task_id, template) | ("hold", reason)"""
    st = packet["scheduler_task"]
    if st.startswith("NEW:"):
        return "dynamic", packet["id"], packet["template"]
    row = tasks.get(st)
    if row is None:
        return "hold", "scheduler_task %s is not in run-state" % st
    if row.get("state") in UNFINISHED:
        return "direct", st
    tpl = template_for(packet)
    if not tpl:
        return "hold", "%s is %s and the %s packet names no template" % (st, row.get("state"), packet["v13_kind"])
    return "dynamic", dynamic_id(packet), tpl


def bound_task(packet, tasks, bindings=None):
    """the task id a packet drives (None while held).  `bindings` {packet: task}
    is the driver's memory: a packet bound direct stays direct after its task
    finishes (bind() alone would offer a dynamic twin)."""
    if bindings and packet["id"] in bindings:
        return bindings[packet["id"]]
    b = bind(packet, tasks)
    return b[1] if b[0] != "hold" else None


def parent_for(packet, tasks, pack, _seen=None):
    """parent_contract_id for a dynamic task, in order of trust: the header's
    `parent_contract`; the existing scheduler_task; a dependency packet's task
    or (recursively) its parent; the id's L-prefix (L08-ADMIT-TG -> L08); the
    first L<nn> the body cites; the parent of a task it closes.  Returns
    (parent, how) -- the driver records `how` and alerts on the guessed forms."""
    _seen = _seen or set()
    if packet.get("parent_contract") and packet["parent_contract"] in tasks:
        return packet["parent_contract"], "header"
    st = packet["scheduler_task"]
    if not st.startswith("NEW:") and st in tasks:
        return st, "scheduler_task"
    for dep in packet["depends_on"]:
        dp = pack.get(dep)
        if not dp or dep in _seen:
            continue
        t = dp["scheduler_task"]
        if not t.startswith("NEW:") and t in tasks:
            return t, "depends_on:%s" % dep
        par, how = parent_for(dp, tasks, pack, _seen | {packet["id"]})
        if par:
            return par, "depends_on:%s>%s" % (dep, how)
    m = re.match(r"^(L\d\d)\b", packet["id"])
    if m and m.group(1) in tasks:
        return m.group(1), "id-prefix"
    m = re.search(r"\b(L\d\d)\b", packet["body"].split("---", 2)[-1])
    if m and m.group(1) in tasks:
        return m.group(1), "body-cite"
    for c in packet["closes"]:
        if c in tasks and (tasks[c].get("parent_contract_id") in tasks):
            return tasks[c]["parent_contract_id"], "closes:%s.parent" % c
    return None, "none"


GUESSED_PARENT = ("body-cite", "closes:")


def dependency_tasks(packet, pack, tasks, bindings=None, gated=()):
    """task ids for `instantiate --depends-on`: every dependency packet's bound
    task (a direct-bound dependency is its own task).  Unbound -> None (wait).
    D23: a dependency packet held by an OWNER GATE whose own scheduler row is
    already accepted (L34 INTEGRATED, its regrade packet behind DELIVERY-4)
    is satisfied by that row -- L34-ACK-DEDUP must not wait on a gate that
    guards a regrade of work already accepted."""
    out = []
    for dep in packet["depends_on"]:
        dp = pack.get(dep)
        if not dp:
            return None
        t = bound_task(dp, tasks, bindings)
        st = dp.get("scheduler_task") or ""
        if (t is None or t not in tasks) and dep in gated and not st.startswith("NEW:") \
                and (tasks.get(st) or {}).get("state") in ACCEPTED:
            t = st
        if t is None or t not in tasks:
            return None
        if t not in out:
            out.append(t)
    return out


def parameters_for(packet, template, parent, candidate=None, covered=None):
    """the template's required_parameters, every value traceable to the packet
    header; reviews bind the registered candidate (sha/tree) or return None."""
    base = packet["base_sha"]
    owned = list(packet["owned_files"])
    common = {"parent_contract_id": parent, "packet_id": packet["id"], "v13_kind": packet["v13_kind"],
              "runner_role": packet["runner_role"], "owned_paths": owned, "closes": list(packet["closes"]),
              "hosted_owed": packet["hosted_owed"], "owner_gate": packet["owner_gate"]}
    if template == "REPAIR":
        return dict(common, defect_id=packet["id"], failing_criterion=packet["title"],
                    reproduction="PACKET.md steps (%s)" % packet["dir"], reviewed_sha=base)
    if template == "TEST_GAP":
        return dict(common, unproved_criterion=packet["title"], candidate_sha=base,
                    test_location=(packet["test_paths"] or owned or ["platform/tests"])[0])
    if template == "EVIDENCE_RECOVERY":
        return dict(common, attempt_ids=[], source_logs=[], candidate_sha=base, evidence_paths=owned)
    if template == "CI_INFRA_REPAIR":
        return dict(common, pipeline_id="", job_id="", failure_log="", reproduction="PACKET.md steps")
    if template == "INTEGRATION_CONFLICT":
        return dict(common, base_sha=base, leaf_shas=[], conflicting_paths=owned, semantic_requirements=packet["title"])
    if template == "EXTERNAL_PREP":
        return dict(common, missing_capability=packet["title"], existing_authority_refs=[],
                    required_fields=owned)
    if template == "DEPENDENCY_IMPLEMENT":
        return dict(common, source_clause=packet["title"], required_interface="", existing_implementation_check="",
                    consumers=[])
    if template in REVIEW_TEMPLATES:
        if not candidate or not candidate.get("sha"):
            return None
        params = dict(common, candidate_sha=candidate["sha"], tree_sha=candidate["tree"],
                      covered_rows=list(covered or []), evidence=[])
        if template == "JUNIOR_REVIEW":
            params.update(diff_or_scope="union:%s" % packet["id"], criteria="catalog")
        elif template == "SECURITY_REVIEW":
            params.update(adversarial_scope=packet["id"])
        else:
            params.update(prior_verdict="", old_sha=base, new_sha=candidate["sha"], change_impact=packet["title"],
                          refreshed_evidence=[])
        return params
    return dict(common)


# -- closure (§6b) -------------------------------------------------------------------------

def closers_of(target, pack):
    return sorted(pid for pid, p in pack.items() if target in p["closes"])


def closure_plan(packet, pack, tasks, bindings=None):
    """after `packet` reached VERIFIED: for each target in its `closes`, retire
    it when every other closer's task is accepted (VERIFIED/INTEGRATED), else
    list it under `pending` with the closers still owed.  A target that is the
    packet's own scheduler_task (L29 closes L29) is a `promote` when it can be
    promoted (REPAIR_REQUIRED/VERIFIED), otherwise a retire."""
    plan = {"retire": [], "promote": [], "pending": {}, "missing": []}
    own = packet["scheduler_task"]
    for target in packet["closes"]:
        if target not in tasks:
            plan["missing"].append(target)
            continue
        others = [c for c in closers_of(target, pack) if c != packet["id"]]
        owed = []
        for c in others:
            t = bound_task(pack[c], tasks, bindings)
            if t is None or tasks.get(t, {}).get("state") not in ACCEPTED:
                owed.append(c)
        if owed:
            plan["pending"][target] = owed
        elif target == own and tasks[target].get("state") in ("REPAIR_REQUIRED", "VERIFIED"):
            plan["promote"].append(target)
        else:
            # a BLOCKED / INVALID_EVIDENCE own task cannot be promoted: it is
            # retired by its V13 successor like any other closed row
            plan["retire"].append(target)
    return plan


HOSTED_TWIN_SUFFIX = "-HOSTED"
HOSTED_ROW_RE = re.compile(r"^-\s*(B\d+)\b.*\[hosted\].*$", re.M)
ROW_GATE_RE = re.compile(r"\(gate: (DELIVERY-[1-5][AB]?|O[1-9]|CIRCLECI)\)")   # check-v13.py GATES_RE
GATE_ROSTER_KEY = {"CIRCLECI": "DELIVERY-1"}    # 00-SCOPE §5: CIRCLECI rows run once DELIVERY-1 is usable
GATE_TEMPLATE = {"CIRCLECI": "TEST_GAP"}        # §19(2): product proof via CircleCI; DELIVERY-*/O* -> EXTERNAL_PREP
TWIN_KIND = "hosted"


def is_hosted_twin(packet):
    """the <ID>-HOSTED[-<GATE>] twin that owes the hosted evidence (00-SCOPE §5,
    PACKET-FORMAT §2/§6); the base packet goes VERIFIED_LOCAL on its [box] rows"""
    return bool(packet.get("twin_of")) or HOSTED_TWIN_SUFFIX in str(packet.get("id") or "")


def hosted_row_gates(text):
    """{row_id: (gate|None, verbatim line)} for every [hosted] row of a BENCHMARK.md"""
    out = {}
    for m in HOSTED_ROW_RE.finditer(text):
        g = ROW_GATE_RE.search(m.group(0))
        out[m.group(1)] = (g.group(1) if g else None, m.group(0).strip())
    return out


def twin_id(parent_id, gate, gates):
    """<ID>-HOSTED when every hosted row shares one gate, else <ID>-HOSTED-<GATE> (§19(1))"""
    return "%s%s" % (parent_id, HOSTED_TWIN_SUFFIX) if len(set(gates)) == 1 \
        else "%s%s-%s" % (parent_id, HOSTED_TWIN_SUFFIX, gate)


def hosted_twins(packet, benchmark_path, lint=None):
    """D37 (PACKET-FORMAT §6, BULK-RULING §19): one synthetic twin packet per
    (packet, gate) over the parent's [hosted] rows.  The twin is held by ITS
    gate only (the header's owner_gate is informational), depends on the
    parent, closes nothing, and is built on the parent's VERIFIED output
    (the driver fills base_sha at instantiation; D28 stacks the worktree).
    A hosted row without a (gate: X) tag is a lint error and joins no twin."""
    if is_hosted_twin(packet) or packet.get("v13_kind") == "review":
        return []
    try:
        text = Path(benchmark_path).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = hosted_row_gates(text)
    if not rows:
        return []
    by_gate = {}
    for rid, (gate, line) in rows.items():
        if gate is None:
            if lint is not None:
                lint.append("ERROR benchmark: %s %s is [hosted] but names no (gate: X); no twin carries it"
                            % (packet["id"], rid))
            continue
        by_gate.setdefault(gate, []).append((rid, line))
    twins = []
    for gate in sorted(by_gate):
        rids = [r for r, _ in by_gate[gate]]
        lines = [l for _, l in by_gate[gate]]
        template = GATE_TEMPLATE.get(gate, "EXTERNAL_PREP")
        tid = twin_id(packet["id"], gate, list(by_gate))
        twins.append({
            "id": tid, "dir": packet["dir"], "title": "%s hosted rows %s behind %s: %s"
            % (packet["id"], ",".join(rids), gate, " | ".join(lines)),
            "v13_kind": TWIN_KIND, "scheduler_task": "NEW:%s" % template, "template": template,
            "runner_role": packet["runner_role"], "closes": [], "depends_on": [packet["id"]],
            "hosted_owed": False, "critical": packet.get("critical", False), "owner_gate": gate,
            "base_sha": "", "owned_files": list(packet["owned_files"]),
            # CIRCLECI twins run the FULL suite off-box: no targeted paths -> suite "full" -> one
            # pipeline per proof (06-ROUTING §5); max_rounds 1 keeps it one pipeline per twin (§19)
            "test_paths": [] if gate == "CIRCLECI" else list(packet["test_paths"]),
            "proof_kind": packet["proof_kind"], "max_rounds": 1 if gate == "CIRCLECI" else packet["max_rounds"],
            "parent_contract": packet.get("parent_contract"), "group": packet.get("group"), "body": packet["body"],
            "review_base": "", "coverage_targets": [],
            "twin_of": packet["id"], "twin_gate": gate, "hosted_rows": rids, "hosted_lines": lines,
        })
    return twins


def gate_open(gate, roster):
    """an owner gate is open only when roster.owner_gates names it true; no map
    = all closed (D34).  CIRCLECI rows open with DELIVERY-1 (00-SCOPE §5)."""
    gates = roster.get("owner_gates")
    if not isinstance(gates, dict):
        return False
    return bool(gates.get(GATE_ROSTER_KEY.get(gate, gate)))


def twin_gate(packet):
    return packet.get("twin_gate") or packet.get("owner_gate") or "none"


def owner_gate_open(packet, roster):
    """D34 (BULK-RULING §14) + D37 (§19(1)): an owner gate holds only a hosted
    twin, and a twin is held by its OWN gate (its rows' `(gate: X)` tag), never
    by the packet header's owner_gate; the base packet dispatches on
    depends_on alone.  A roster with no `owner_gates` map is all-closed for
    twins, never silently open."""
    if not is_hosted_twin(packet):
        return True
    gate = twin_gate(packet)
    if gate == "none":
        return True
    return gate_open(gate, roster)


def twin_benchmark(packet):
    """the twin's .vp/BENCHMARK.md: exactly its hosted rows, verbatim (§6: its
    benchmark IS the [hosted] rows of the parent)"""
    return ("# %s -- hosted rows of %s behind %s (PACKET-FORMAT-v13 §6)\n\n%s\n"
            % (packet["id"], packet["twin_of"], packet["twin_gate"], "\n".join(packet["hosted_lines"])))


TWIN_PREAMBLE = """<!-- HOSTED TWIN {id} (PACKET-FORMAT-v13 §6, BULK-RULING §19) -->
> **This is the hosted twin of `{parent}` for gate `{gate}`.** The parent's `[box]` rows are already VERIFIED
> at `{base}`; this worktree is cut from exactly that commit (`.vp/BASE`), never trunk. Prove ONLY the rows in
> `.vp/BENCHMARK.md` ({rows}); every other row of the parent benchmark is out of scope here. Do not re-do the
> parent's product work: the owned files below are the parent's, listed so the proof scope is unchanged.
{how}
> Rows: {lines}

"""
TWIN_HOW = {
    "CIRCLECI": "> Proof: the driver runs the FULL CircleCI pipeline on this exact sha as the proof (06-ROUTING §5,"
                " one pipeline per twin); grade each row from the proof record and the pipeline artefacts.",
    "DELIVERY-2A": "> Host: the Oracle VPS is reachable over SSH/sudo/Docker per `voice-pod/deployment/VPS_HANDOVER.md`"
                   " §1-2 (DELIVERY-2A). Rehearsal secret policy (§18 ruling, standing default): agent-generated"
                   " values per VPS_HANDOVER.md §5, placeholder strings for the 3 owner API keys, hostname-coupled"
                   " vars = voicepod-vps.internal, ACME off, deploy.env never committed, everything discarded before"
                   " the real DELIVERY-2B deploy.",
}


TWIN_HEADER_OVERRIDES = ("item", "title", "depends_on", "test_paths", "closes", "hosted_owed", "owner_gate",
                         "max_rounds", "base_sha", "twin_of", "twin_gate", "hosted_rows")


def _yaml_list(key, values):
    return ["%s: []" % key] if not values else ["%s:" % key] + ["  - %s" % v for v in values]


def twin_front_matter(packet, base):
    """the twin's front matter: the parent's, with the twin's own item/title/
    depends_on/test_paths/base_sha/owner_gate/max_rounds (the driver's proof
    step and base rule read THIS header: no test_paths -> the full suite ->
    CircleCI, 06-ROUTING §5)"""
    body = packet["body"]
    lines = body.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, body
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None, body
    drop = set(TWIN_HEADER_OVERRIDES) | {"proof_only"}   # D98: a twin is the full suite, never one job/leg
    if not packet["test_paths"]:
        drop |= {"proof_paths", "proof_workers"}   # a full-suite twin keeps no box narrowing (F-B)
    kept, skip = [], False
    for ln in lines[1:end]:
        key = ln.split(":", 1)[0].strip() if ln and not ln.startswith((" ", "\t", "-")) else None
        if key is not None:
            skip = key in drop
        if not skip:
            kept.append(ln)
    kept += ["item: %s" % packet["id"], "title: %s" % json.dumps(packet["title"][:200]),
             "base_sha: %s" % base, "owner_gate: %s" % packet["twin_gate"], "hosted_owed: false",
             "max_rounds: %d" % packet["max_rounds"], "twin_of: %s" % packet["twin_of"],
             "twin_gate: %s" % packet["twin_gate"]]
    kept += _yaml_list("depends_on", packet["depends_on"]) + _yaml_list("test_paths", packet["test_paths"])
    kept += _yaml_list("closes", []) + _yaml_list("hosted_rows", packet["hosted_rows"])
    return "---\n" + "\n".join(kept) + "\n---\n", "\n".join(lines[end + 1:])


def twin_packet_text(packet, base):
    """the twin's .vp/PACKET.md: the twin's front matter, the §6 preamble, then
    the parent's packet body"""
    how = TWIN_HOW.get(packet["twin_gate"], "> Gate `%s` is open per roster owner_gates; the row text names the"
                       " external system to prove against." % packet["twin_gate"])
    pre = TWIN_PREAMBLE.format(id=packet["id"], parent=packet["twin_of"], gate=packet["twin_gate"], base=base,
                               rows=",".join(packet["hosted_rows"]), how=how,
                               lines=" | ".join(packet["hosted_lines"]))
    fm, body = twin_front_matter(packet, base)
    return (fm or "") + pre + body


def dispatch_record(packet, task, mode, template, parent, base, extra=None):
    rec = {"packet": packet["id"], "task": task, "mode": mode, "template": template,
           "parent_contract_id": parent, "v13_kind": packet["v13_kind"], "runner_role": packet["runner_role"],
           "base_sha": base, "closes": list(packet["closes"]), "depends_on": list(packet["depends_on"]),
           "owner_gate": packet["owner_gate"], "hosted_owed": packet["hosted_owed"],
           "proof_kind": packet["proof_kind"], "owned_files": list(packet["owned_files"])}
    rec.update(extra or {})
    return rec


def substitute_union(packet, union):
    """REVIEW-* packets carry `<union>` in owned_files; the driver's dispatch
    record assigns the union id and substitutes it before the turn."""
    return [f.replace("<union>", union) for f in packet["owned_files"]]


def topo_order(pack):
    """packets in dependency order (a packet after everything it depends on)"""
    out, seen = [], set()

    def visit(pid, stack):
        if pid in seen or pid not in pack:
            return
        if pid in stack:
            return
        for dep in pack[pid]["depends_on"]:
            visit(dep, stack | {pid})
        seen.add(pid)
        out.append(pid)
    for pid in sorted(pack):
        visit(pid, set())
    return out


if __name__ == "__main__":  # pragma: no cover
    import sys
    pack, lint = load_pack(sys.argv[1])
    print(json.dumps({"packets": len(pack), "lint": lint, "order": topo_order(pack)}, indent=2))
