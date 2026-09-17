#!/usr/bin/env python3
"""lanedryrun.py -- bootstrap a throwaway v13 run root for lanedriver dry runs.

    python3 lanedryrun.py init --root <dir> --trunk <repo> --catalog <lane-contracts.json> \
        --control <orchestration_control.py> [--pack-dir <03-PACKETS>] [--run-id <id>]

Clones the trunk (read-only on the source), copies the catalog, writes a
roster.json (all OpenCode roles on go2, reviewers on codex), and runs
init / bind-chief / satisfy-gate x4 / activate on a fresh run-state.json.
Nothing touches the source repository or the live scheduler state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def sh(argv, cwd=None):
    cp = subprocess.run([str(a) for a in argv], cwd=cwd, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if cp.returncode:
        raise SystemExit("FAILED %s\n%s\n%s" % (" ".join(str(a) for a in argv), cp.stdout, cp.stderr))
    return cp.stdout


def roster(root, trunk, control, state, catalog, pack_dir, control_cwd):
    oc = lambda model, variant: {"runner": "opencode", "model": model, "variant": variant, "server": "go2"}  # noqa: E731
    muse = "opencode-go/muse-spark-1.3-contributor"
    ds = "opencode-go/deepseek-v4.1-flash"
    return {
        "run": {"cn": str(root), "trunk": str(trunk), "worktrees": str(root / "vp-worktrees"),
                "pack_dir": str(pack_dir) if pack_dir else None, "stop_grace_s": 30},
        "control": {"python": "python3", "script": str(control), "cwd": str(control_cwd),
                    "state": str(state), "catalog": str(catalog)},
        "night": {"pause_on_disk_gb": 25},
        "concurrency": {"max_rounds": 3, "codex_max": 3, "claude_max": 2, "agy_max": 2,
                        "max_tasks_in_flight": 12,
                        "max_minutes_per_turn": {"builder": 45, "grader": 20, "probe": 30,
                                                 "reviewer": 45, "default": 30}},
        "budget": {"max_cost_usd_per_run": 60},
        "alerts": {"frontier_every_s": 60, "idle_every_min": 30},
        "servers": {"go2": {"url": "http://127.0.0.1:4102", "max_concurrent": 8,
                            "xdg_data_home": str(root / "xdg-go2")},
                    "go1": {"url": "http://127.0.0.1:4101", "max_concurrent": 3, "parked": True},
                    "go3": {"url": "http://127.0.0.1:4103", "max_concurrent": 3, "parked": True}},
        "roles": {"builder": oc(muse, "xhigh"), "control": oc(muse, "xhigh"),
                  "security_build": oc(muse, "xhigh"), "integration": oc(muse, "xhigh"),
                  "grader": oc(ds, "high"), "probe": oc(ds, "high"), "design": oc(ds, "high"),
                  "verification": oc(ds, "high"), "operations": oc(ds, "high"),
                  "provider": oc(ds, "high"),
                  "junior": {"runner": "codex", "model": "gpt-5.6-luna", "effort": "xhigh"},
                  "security": {"runner": "codex", "model": "gpt-5.6-sol", "effort": "high"},
                  "final_review": {"runner": "codex", "model": "gpt-5.6-sol", "effort": "high"},
                  "adjudicator": {"runner": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"}},
    }


def cmd_init(args):
    root = Path(args.root).resolve()
    (root / "control" / "orchestration-state").mkdir(parents=True, exist_ok=True)
    (root / "run").mkdir(parents=True, exist_ok=True)
    trunk = root / "trunk"
    if not trunk.exists():
        sh(["git", "clone", "-q", args.trunk, str(trunk)])
    catalog = root / "control" / "lane-contracts.json"
    catalog.write_bytes(Path(args.catalog).read_bytes())
    evidence = root / "control" / "evidence.json"
    evidence.write_text('{"ok": true, "note": "throwaway dry run"}\n')
    state = root / "control" / "orchestration-state" / "run-state.json"
    control = Path(args.control).resolve()
    ctl = ["python3", str(control), "--state", str(state), "--catalog", str(catalog)]
    if not state.exists():
        sh(ctl + ["init", "--run-id", args.run_id], cwd=str(control.parent))
        for chief in ("A", "B"):
            sh(ctl + ["bind-chief", "--chief", chief, "--thread-id", "lanedriver"], cwd=str(control.parent))
        for gate in ("PRESERVATION_COMPLETE", "REQUIREMENTS_REBOUND", "RECOVERY_ADOPTED", "DRY_RUN_PASSED"):
            sh(ctl + ["satisfy-gate", "--name", gate, "--evidence", str(evidence)], cwd=str(control.parent))
        sh(ctl + ["activate"], cwd=str(control.parent))
    rpath = root / "run" / "roster.json"
    if not rpath.exists() or args.force_roster:
        rpath.write_text(json.dumps(roster(root, trunk, control, state, catalog,
                                           Path(args.pack_dir).resolve() if args.pack_dir else None,
                                           control.parent), indent=2) + "\n")
    ready = json.loads(sh(ctl + ["ready"], cwd=str(control.parent)))
    print(json.dumps({"root": str(root), "roster": str(rpath), "phase": ready["phase"],
                      "ready": [r["task_id"] for r in ready["ready"]]}, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--root", required=True)
    i.add_argument("--trunk", required=True)
    i.add_argument("--catalog", required=True)
    i.add_argument("--control", required=True)
    i.add_argument("--pack-dir")
    i.add_argument("--run-id", default="dryrun")
    i.add_argument("--force-roster", action="store_true")
    i.set_defaults(func=cmd_init)
    args = ap.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
