#!/bin/zsh
# Every hermetic check for dispatcher/**, in one command. Exit 0 only if all of them pass.
set -u
HERE=${0:A:h}
PY=/usr/bin/python3
rc=0

# --- every test file in this directory must be RUN by this script ------------------------------
# 2026-09-06 21:47: a test file landed here and was not added below. An unwired test is invisible —
# the suite goes green without it and nothing says a check is missing, which is the same
# absence-reads-as-a-pass shape the tests themselves exist to catch. I caught that one by
# remembering to look; this makes it impossible to miss instead.
# The census runs FIRST and does not stop the suite: knowing about a hole should not cost you the
# results of everything that is wired.
unwired=()
# (N) is the null_glob qualifier: without it zsh ABORTS the script on a directory with no
# test files, which is a crash where the honest answer is "nothing to census".
for f in "$HERE"/test_*.py(N); do
  grep -q "${f:t}" "$0" || unwired+=("${f:t}")
done
if (( ${#unwired} )); then
  rc=1
  print "\n=== UNWIRED TEST FILES — these exist and this runner does NOT run them ==="
  for u in $unwired; do print "  $u"; done
  print "Add each to run_all.sh, or delete it. A test file nobody runs is not a test."
fi
if [[ ${1:-} == --census-only ]]; then
  (( rc == 0 )) && print "census: all test files are wired"
  exit $rc
fi
run() {
  print "\n=== $1 ==="
  shift
  "$@" || rc=1
}
run "test_runner_census.py (run_all.sh)"  $PY "$HERE/test_runner_census.py"
run "test_fixes.py (dispatcher.py)"        $PY "$HERE/test_fixes.py"
run "test_cutmarker.py (dispatcher.py)"    $PY "$HERE/test_cutmarker.py"
run "test_autogate.py (autogate.py)"     $PY "$HERE/test_autogate.py"
run "test_autogate_driver.py (disp)"     $PY "$HERE/test_autogate_driver.py"
run "test_manualgate.py (ctl+disp)"     $PY "$HERE/test_manualgate.py"
run "test_restartdrop.py (disp)"       $PY "$HERE/test_restartdrop.py"
run "test_gatesilence.py (disp)"       $PY "$HERE/test_gatesilence.py"
run "test_laneheld.py (disp)"          $PY "$HERE/test_laneheld.py"
run "test_codexpath.py (gate)"         $PY "$HERE/test_codexpath.py"
run "test_precheck.py (ctl+gate)"      $PY "$HERE/test_precheck.py"
run "test_reportedflip.py (disp)"      $PY "$HERE/test_reportedflip.py"
run "test_planclassify.py (disp)"      $PY "$HERE/test_planclassify.py"
run "test_waitongate.py (disp)"        $PY "$HERE/test_waitongate.py"
run "test_packwhole.py (agy)"          $PY "$HERE/test_packwhole.py"
run "test_queueskip.py (disp)"         $PY "$HERE/test_queueskip.py"
run "test_deadexec.py (dispatcher)"      $PY "$HERE/test_deadexec.py"
run "test_questiontool.py (dispatcher)"  $PY "$HERE/test_questiontool.py"
run "test_compaction.py (dispatcher.py)"   $PY "$HERE/test_compaction.py"
run "test_stalerow.py (dispatcher.py)"     $PY "$HERE/test_stalerow.py"
run "test_clearrow.py (ctl + dispatcher)" $PY "$HERE/test_clearrow.py"
run "test_gates_listing.py (ctl)"      $PY "$HERE/test_gates_listing.py"
run "test_laneoccupied.py (dispatcher)"   $PY "$HERE/test_laneoccupied.py"
run "test_execlabel.py (dispatcher)"     $PY "$HERE/test_execlabel.py"
run "test_preflight.py (mergegate.py)"     $PY "$HERE/test_preflight.py"
run "test_portal_row.py (mergegate.py)"    $PY "$HERE/test_portal_row.py"
run "test_reportclock.py (mergegate.py)"   $PY "$HERE/test_reportclock.py"
run "test_yamlparse.py (mergegate.py)"     $PY "$HERE/test_yamlparse.py"
run "test_sweepworktree.py (mergegate)"    $PY "$HERE/test_sweepworktree.py"
run "test_missingvenv.py (mergegate)"      $PY "$HERE/test_missingvenv.py"
run "test_gate_survives.py (mergegate)"    $PY "$HERE/test_gate_survives.py"
run "test_wallskip.py (mergegate)"       $PY "$HERE/test_wallskip.py"
run "test_codexwall.py (mergegate)"     $PY "$HERE/test_codexwall.py"
run "test_gate_crashed.py (mergegate)"  $PY "$HERE/test_gate_crashed.py"
run "test_gate_md.py (mergegate.py)"       $PY "$HERE/test_gate_md.py"
run "test_restartguard.py (ctl)"         $PY "$HERE/test_restartguard.py"
run "test_migcollide.py (mergegate)"     $PY "$HERE/test_migcollide.py"
run "test_mutate.py (mutate.py)"          $PY "$HERE/test_mutate.py"
run "test_undeclared.py (gate rows)"      $PY "$HERE/test_undeclared.py"
run "test_reportproofs.py (mergegate)"    $PY "$HERE/test_reportproofs.py"
run "test_siblingmig.py (mergegate)"      $PY "$HERE/test_siblingmig.py"
run "test_prooftimeout.py (mergegate)"    $PY "$HERE/test_prooftimeout.py"
run "test_worklabel.py (dispatcher)"      $PY "$HERE/test_worklabel.py"
run "test_pausedview.py (dispatcher)"     $PY "$HERE/test_pausedview.py"
run "test_observe.py (dispatcher)"        $PY "$HERE/test_observe.py"
run "test_feedidle.py (dispatcher)"       $PY "$HERE/test_feedidle.py"
run "test_noturn.py (dispatcher)"         $PY "$HERE/test_noturn.py"
run "test_reportheld.py (dispatcher)"     $PY "$HERE/test_reportheld.py"
run "test_nullwt.py (dispatcher)"         $PY "$HERE/test_nullwt.py"
run "test_astraroute.py (mergegate)"      $PY "$HERE/test_astraroute.py"
run "test_muse.py (museadapter)"          $PY "$HERE/test_muse.py"
run "test_musesession.py (conversation identity)" $PY "$HERE/test_musesession.py"
run "test_muserunner.py (live evidence)" $PY "$HERE/test_muserunner.py"
run "test_circleaccount.py (account identity)" $PY "$HERE/test_circleaccount.py"
run "test_sessionstatus.py (exact turn evidence)" $PY "$HERE/test_sessionstatus.py"
run "test_legacyenv.py (legacy strip)"    $PY "$HERE/test_legacyenv.py"
run "test_codexroute.py (spawn wiring)"   $PY "$HERE/test_codexroute.py"
run "test_routewindow.py (route by window)" $PY "$HERE/test_routewindow.py"
run "test_profileroot.py (profile root)"   $PY "$HERE/test_profileroot.py"
run "test_ctlself.py (ctl restart)"       $PY "$HERE/test_ctlself.py"
run "test_clearhold.py (ctl clear-hold)"  $PY "$HERE/test_clearhold.py"
run "test_sessionwatch.py (sessions)"     $PY "$HERE/test_sessionwatch.py"
run "test_pglock.py (pgserver lock)"      $PY "$HERE/test_pglock.py"
run "test_pgpopulation.py (box runs)"     $PY "$HERE/test_pgpopulation.py"
run "test_serverdown.py (degraded tick)" $PY "$HERE/test_serverdown.py"
run "test_boxlock.py (mergegate)"        $PY "$HERE/test_boxlock.py"
run "test_portallock.py (mergegate)"     $PY "$HERE/test_portallock.py"
run "test_answer.py (ctl+disp)"          $PY "$HERE/test_answer.py"
run "test_notrun.py (gate+autogate)"    $PY "$HERE/test_notrun.py"
run "test_checkpoint.py (checkpoint)"   $PY "$HERE/test_checkpoint.py"
run "test_citesweep_zero.py (citesweep)"   $PY "$HERE/test_citesweep_zero.py"
run "test_ledgertrunk.py (citesweep)"    $PY "$HERE/test_ledgertrunk.py"
run "citesweep.py --selftest"              $PY "$HERE/../citesweep.py" --selftest
run "lockwatch.py --selftest"              $PY "$HERE/../lockwatch.py" --selftest
# gatereview2's packer half only: the full --selftest spends a model call.
run "test_agydirs.py (gatereview2)"     $PY "$HERE/test_agydirs.py"
run "test_agynoreview.py (gatereview2)"  $PY "$HERE/test_agynoreview.py"
run "test_packfull.py (gatereview2)"     $PY "$HERE/test_packfull.py"
run "gatereview2.selftest_pack()"          $PY -c "import sys; sys.path.insert(0, '$HERE/..'); import gatereview2; sys.exit(0 if gatereview2.selftest_pack() else 1)"
if (( ${#unwired} )); then
  print "\n=== UNWIRED (repeated, so it is not scrolled away): ${(j:, :)unwired} ==="
fi
print "\n=== dispatcher selftest: $([[ $rc == 0 ]] && print ALL GREEN || print FAILURES ABOVE) ==="
exit $rc
