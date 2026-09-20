"""Bot scenario for the save-compatibility gate: a Node/mineflayer script drives players.

Console commands can only reach the part of a plugin that accepts a non-player sender
(for Medieval Factions: leaderless factions and nothing else). Claims, alliances,
locked blocks and gates need a Player in the world, so a scenario script joins bots to
the OMCSI server (offline mode is on) and plays them, reading each step back over chat
and RCON. The script's contract:

  * it is a single Node file fetched from `scenario_script` (a raw URL, normally a file
    in Dans-Plugins/plugin-fixtures) and run with
      node <script> --host localhost --port <mc port> --rcon-port <rcon port>
                    --rcon-password <the run's RCON_PASSWORD> --bots 2
                    --server-log docker:<container> --json-out <path>
  * `mineflayer` is installed next to it (SCENARIO_NODE_DIR), so `require('mineflayer')`
    resolves;
  * it exits 0 only when every step verified, non-zero otherwise;
  * its stdout is the evidence and is captured into the run log and
    evidence/bot-scenario.log;
  * its last stdout line may be `SCENARIO_EXPECTED {json}`: a count per persisted entity
    type keyed by the label the plugin prints in its `<n> <label> loaded` startup lines.
    When present, the baseline's restart counts must match it (bot-scenario-counts).

Two assertions are recorded (both "none" when no script is given):

  bot-scenario         the script exits 0 within the timeout, and no error is attributable
                       to the plugin in the console while it runs
  bot-scenario-counts  every label the script expects that the baseline logs on restart
                       carries the expected count

Environment:
  SCENARIO_SCRIPT_URL   raw URL of the script (empty: the assertions are "none")
  SCENARIO_NODE_DIR     directory where `npm install mineflayer` was run (required if a
                        script is given)
  SCENARIO_MC_PORT      default 25565      (compose maps HOST_PORT -> 25565)
  SCENARIO_RCON_PORT    default 25575      (compose maps HOST_RCON_PORT -> 25575)
  RCON_PASSWORD         the password in the wrapper's .env (required if a script is given)
  SCENARIO_TIMEOUT      seconds, default 900
"""

import json
import os
import re
import shutil
import subprocess
import time

import requests

SCRIPT_URL = os.getenv("SCENARIO_SCRIPT_URL") or None
NODE_DIR = os.getenv("SCENARIO_NODE_DIR") or None
MC_PORT = os.getenv("SCENARIO_MC_PORT", "25565")
RCON_PORT = os.getenv("SCENARIO_RCON_PORT", "25575")
RCON_PASSWORD = os.getenv("RCON_PASSWORD") or None
TIMEOUT = int(os.getenv("SCENARIO_TIMEOUT", "900"))

EXPECTED_LINE = re.compile(r"^SCENARIO_EXPECTED (\{.*\})\s*$", re.M)


def run_bot_scenario(record, result, work_dir, container, plugin_name, package_prefix,
                     now_cursor, logs_since, attributable_errors):
    """Fetch and run the script against the running baseline; record `bot-scenario`.

    `record(name, passed, detail)` is the gate's own reporter (it exits on failure);
    `result` is the gate's result dict (`scenarioExpected` and `scenarioScript` are added).
    """
    if not SCRIPT_URL:
        record("bot-scenario", True, "none")
        return
    if not NODE_DIR or not os.path.isdir(os.path.join(NODE_DIR, "node_modules", "mineflayer")):
        record("bot-scenario", False, "SCENARIO_NODE_DIR has no node_modules/mineflayer (workflow did not install it)")
    if not RCON_PASSWORD:
        record("bot-scenario", False, "RCON_PASSWORD is not set; the script cannot read state back")

    result["scenarioScript"] = SCRIPT_URL
    script_dir = os.path.join(work_dir, "scenario")
    os.makedirs(script_dir, exist_ok=True)
    script = os.path.join(script_dir, os.path.basename(SCRIPT_URL.split("?")[0]) or "scenario.js")
    try:
        resp = requests.get(SCRIPT_URL, timeout=60)
        resp.raise_for_status()
    except Exception as exc:
        record("bot-scenario", False, f"script could not be fetched from {SCRIPT_URL}: {exc}")
    with open(script, "wb") as f:
        f.write(resp.content)
    # The script resolves `mineflayer` from its own directory upward, so it runs from
    # where the modules were installed.
    runnable = os.path.join(NODE_DIR, os.path.basename(script))
    shutil.copyfile(script, runnable)

    evidence_dir = os.path.join(work_dir, "evidence")
    os.makedirs(evidence_dir, exist_ok=True)
    json_out = os.path.join(evidence_dir, "bot-scenario.json")
    log_path = os.path.join(evidence_dir, "bot-scenario.log")
    cmd = [
        "node", runnable,
        "--host", "localhost", "--port", MC_PORT,
        "--rcon-port", RCON_PORT, "--rcon-password", RCON_PASSWORD,
        "--bots", "2",
        "--server-log", f"docker:{container}",
        "--json-out", json_out,
    ]
    shown = [("<rcon password>" if a == RCON_PASSWORD else a) for a in cmd]
    print(f"  $ {' '.join(shown)}")
    cursor = now_cursor()
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=NODE_DIR, capture_output=True, text=True, timeout=TIMEOUT)
        output = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else "")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")) \
            + "\n[harness] scenario timed out\n"
        code = None
    elapsed = int(time.time() - started)
    with open(log_path, "w") as f:
        f.write(output)
    for line in output.splitlines():
        print("    | " + line)

    if code is None:
        record("bot-scenario", False, f"script did not finish within {TIMEOUT}s")
    if code != 0:
        summary = [l.strip() for l in output.splitlines() if l.strip().startswith("FAIL")]
        record("bot-scenario", False, f"script exited {code} after {elapsed}s"
               + (f": {summary[-1][:300]}" if summary else ""))

    # Let the plugin's writes for the bots' quits land before the console is judged.
    time.sleep(3)
    errors = attributable_errors(logs_since(cursor), plugin_name, package_prefix)
    if errors:
        record("bot-scenario", False, "console during the scenario: " + "; ".join(errors[:5]))

    m = EXPECTED_LINE.search(output)
    expected = None
    if m:
        try:
            expected = {str(k): int(v) for k, v in json.loads(m.group(1)).items()}
        except (ValueError, TypeError, AttributeError) as exc:
            record("bot-scenario", False, f"SCENARIO_EXPECTED line is not a label->integer object: {exc}")
    result["scenarioExpected"] = expected
    steps = len([l for l in output.splitlines() if l.strip().startswith("PASS")])
    record("bot-scenario", True, f"script exited 0 after {elapsed}s ({steps} PASS line(s))"
           + (f"; expects {expected}" if expected else "; no SCENARIO_EXPECTED line"))


def check_expected_counts(record, result, baseline_counts, initial_counts=None):
    """Record `bot-scenario-counts`: what the script said it would create equals what the
    baseline's restart counts gained over `initial_counts` — the counts its first boot
    loaded (zero on a fresh server; the supplied fixture's contents otherwise). A script's
    SCENARIO_EXPECTED describes what the script creates, not the whole store."""
    expected = result.get("scenarioExpected")
    if not SCRIPT_URL:
        record("bot-scenario-counts", True, "none")
        return
    if not expected:
        record("bot-scenario-counts", True, "script printed no SCENARIO_EXPECTED line; nothing to compare")
        return
    initial = initial_counts or {}
    added = {l: baseline_counts[l] - initial.get(l, 0) for l in expected if l in baseline_counts}
    mismatched = {l: (expected[l], added[l]) for l in added if added[l] != expected[l]}
    not_logged = sorted(l for l in expected if l not in baseline_counts)
    matched = {l: added[l] for l in added if l not in mismatched}
    detail = f"added by the scenario {matched}" + (f" over the fixture's {initial}" if any(initial.values()) else "")
    if not_logged:
        detail += f"; not logged by the baseline {not_logged}"
    if mismatched:
        record("bot-scenario-counts", False, "mismatch (expected, added): " + str(mismatched) + "; " + detail)
    record("bot-scenario-counts", True, detail)
