#!/usr/bin/env python3
"""Run CircleCI with an identity-checked, account-specific keychain credential.

Config filenames do not isolate the CLI's default keyring. This entry point never
uses that default or logs a credential. It refuses commands on identity mismatch.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

EXPECTED = {
    "1": "2022bdb8-1732-4a40-873c-33c89c98a6e1",
    "2": "05153de2-0892-44f9-b5c2-c6948d6678d4",
    "3": "9d9a0128-b6b8-479e-9e1c-048e4157d8b1",
    "A1": "649ae2e6-64e7-4abe-bc7a-6aafc538a9df",
    "A2": "375f56c9-2c16-4f99-851c-79182aadd6ce",
    "A3": "31b463e7-ddeb-44bd-ab37-89a113cb68b3",
}


def verified_env(account, *, base=None, run=subprocess.run, binary=None):
    if account not in EXPECTED:
        raise ValueError("Unknown CircleCI account")
    binary = binary or shutil.which("circleci")
    if not binary:
        raise RuntimeError("CircleCI CLI is not installed")
    secret = run(["security", "find-generic-password", "-s",
                  f"voicepod-circleci-account-{account}", "-w"],
                 capture_output=True, text=True, timeout=20)
    if secret.returncode or not secret.stdout.strip():
        raise RuntimeError(f"Account {account}: dedicated keychain credential unavailable")
    env = dict(os.environ if base is None else base)
    # Never allow an inherited server override to receive the selected credential.
    env["CIRCLE_HOST"] = "https://circleci.com"
    env["CIRCLE_TOKEN"] = secret.stdout.strip()
    env["CIRCLE_NO_INTERACTIVE"] = "1"
    env["CIRCLE_NO_UPDATE_CHECK"] = "1"
    config = str(Path.home() / ".config" / f"circleci-account-{account}" / "config.yml")
    prefix = [binary, "-c", config]
    identity = run(prefix + ["auth", "me", "--json"], env=env,
                   capture_output=True, text=True, timeout=30)
    if identity.returncode:
        # D82: carry the CLI's own words (rate limit, 5xx, network) -- 2026-09-19
        # two canary polls died on this line and the log said nothing more
        detail = ((identity.stderr or "") + " " + (identity.stdout or "")).strip().replace("\n", " ")[-200:]
        raise RuntimeError(f"Account {account}: identity request failed; command was not run"
                           + (f" ({detail})" if detail else ""))
    try:
        actual = json.loads(identity.stdout)["id"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(f"Account {account}: invalid identity response") from None
    if actual != EXPECTED[account]:
        raise RuntimeError(f"Account {account}: identity mismatch; command was not run")
    return prefix, env, actual


def execute(account, command, *, run=subprocess.run, base=None, binary=None):
    # Login/settings mutations would restore the shared-keyring ambiguity.
    allowed = {"org", "project", "pipeline", "run", "workflow", "job", "artifact", "testresult", "api", "config"}
    forbidden = {"--config", "--debug", "--host", "--hostname", "--token", "--insecure-storage"}
    if command and (command[0] not in allowed
                    or any(x.split("=", 1)[0] in forbidden or x.startswith("-c")
                           for x in command)
                    or (command[0] == "api" and any("://" in x or x.startswith("//") for x in command[1:]))):
        raise ValueError("Use this launcher for CI work, not login/settings/config overrides")
    prefix, env, identity = verified_env(account, base=base, run=run, binary=binary)
    if not command:
        print(json.dumps({"account": account, "id": identity, "identity_verified": True}))
        return 0
    return run(prefix + command, env=env).returncode


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("account", choices=EXPECTED)
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args()
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    try:
        return execute(a.account, command)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
        # TimeoutExpired may contain command arguments, but never the child environment.
        print(f"CircleCI account check refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
