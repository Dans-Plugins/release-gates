#!/usr/bin/env python3
"""Boot gate (T1): prove a candidate plugin jar boots on a real Spigot server, twice.

Drives an OMCSI `minecraft-wrapper` container through its REST API (the same surface
Dan's Plugin Manager's integration test uses) and reads the server's console from
`docker logs`. It asserts, in order:

  dependencies   every `depend:` in the candidate's plugin.yml is satisfied by a supplied jar
  boot-1         the server reaches "Done"; the candidate reports Enabling; no enable failure;
                 no ERROR/SEVERE line or stack frame attributable to the candidate
  version        the version the candidate enables with is the one expected (when given)
  plugins-1      `plugins` lists the candidate
  commands-1     `help <command>` for every command in plugin.yml is answered with a help
                 topic, not "No help for" / "Unknown command"
  stop-1         the server stops within the timeout and the candidate disables without error
  boot-2         a second boot over the data folder the first boot created; same checks
  plugins-2
  commands-2
  stop-2

It stops at the first assertion that fails, writes `result.json`, and exits non-zero.

Environment:
  CANDIDATE_JAR        path to the candidate jar (required)
  DEPENDENCY_JARS      newline-separated paths of dependency jars (optional)
  EXPECTED_VERSION     the version string the candidate must enable with (optional)
  REPOSITORY, SHA      recorded in result.json only
  RESULT_PATH          where to write result.json (default: result.json)
  OMCSI_API_BASE       default http://localhost:8092
  OMCSI_DEPLOY_TOKEN   bearer token for /api/plugins/deploy (required)
  OMCSI_CONTAINER_NAME default open-mc-server
"""

import datetime
import json
import os
import re
import subprocess
import sys
import time
import zipfile

import requests
import yaml

API_BASE = os.getenv("OMCSI_API_BASE", "http://localhost:8092")
TOKEN = os.environ["OMCSI_DEPLOY_TOKEN"]
CONTAINER = os.getenv("OMCSI_CONTAINER_NAME", "open-mc-server")
CANDIDATE_JAR = os.environ["CANDIDATE_JAR"]
DEPENDENCY_JARS = [p for p in os.getenv("DEPENDENCY_JARS", "").split("\n") if p.strip()]
EXPECTED_VERSION = os.getenv("EXPECTED_VERSION") or None
RESULT_PATH = os.getenv("RESULT_PATH", "result.json")

_HEADERS = {"Authorization": f"Bearer {TOKEN}"}

RESULT = {
    "gate": "boot",
    "repository": os.getenv("REPOSITORY"),
    "sha": os.getenv("SHA"),
    "candidate": os.path.basename(CANDIDATE_JAR),
    "plugin": None,
    "version": None,
    "passed": False,
    "assertions": [],
    "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "finishedAt": None,
}


# --- reporting ---------------------------------------------------------------------------

def record(name, passed, detail=""):
    RESULT["assertions"].append({"name": name, "passed": passed, "detail": detail})
    print(f"  {'PASS' if passed else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))
    if not passed:
        finish(False)


def finish(passed):
    RESULT["passed"] = passed
    RESULT["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(RESULT_PATH, "w") as f:
        json.dump(RESULT, f, indent=2)
    print(f"\n=== boot gate {'PASSED' if passed else 'FAILED'} ===")
    sys.exit(0 if passed else 1)


# --- OMCSI -------------------------------------------------------------------------------

def _api(method, path, **kwargs):
    resp = requests.request(method, f"{API_BASE}{path}", headers=_HEADERS, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


def is_running():
    try:
        return bool(_api("GET", "/api/server/status").json().get("running"))
    except Exception:
        return False


def wait_for(predicate, timeout, label, poll=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        print(f"  waiting for {label} ({int(deadline - time.time())}s left)...")
        time.sleep(poll)
    return False


def now_cursor():
    # One second back so lines emitted in the same second as the action are included.
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)


def logs_since(cursor):
    cmd = ["docker", "logs", "--since", cursor.isoformat(), CONTAINER]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return r.stdout + r.stderr


def send_command(cmd):
    requests.post(
        f"{API_BASE}/api/server/command",
        headers={**_HEADERS, "Content-Type": "text/plain; charset=utf-8"},
        data=cmd.encode("utf-8"),
        timeout=30,
    ).raise_for_status()
    print(f"  > {cmd}")


def deploy_jar(path):
    name = os.path.basename(path)
    with open(path, "rb") as jar:
        _api(
            "POST",
            "/api/plugins/deploy",
            data={"pluginName": name},
            files={"file": (name, jar, "application/java-archive")},
        )
    print(f"  deployed {name}")


# --- plugin.yml --------------------------------------------------------------------------

def read_plugin_yml(jar_path):
    with zipfile.ZipFile(jar_path) as z:
        with z.open("plugin.yml") as f:
            return yaml.safe_load(f) or {}


# --- assertions --------------------------------------------------------------------------

ERROR_LINE = re.compile(r"/(ERROR|SEVERE)\]")
DONE_LINE = re.compile(r"Done \([\d.]+s\)!")
# Bukkit's HelpCommand answers `help <topic>` with a "Help: /<topic>" header when the
# topic exists and "No help for <topic>" when it does not.
HELP_MISSING = re.compile(r"No help for \S+|Unknown command")
HELP_ANSWERED = re.compile(r"Help: |No help for \S+|Unknown command")


def attributable_errors(log, plugin_name, package_prefix):
    """ERROR/SEVERE lines that name the plugin, plus stack frames inside its package."""
    hits = []
    for line in log.splitlines():
        if ERROR_LINE.search(line) and (f"[{plugin_name}]" in line or plugin_name in line):
            hits.append(line.strip())
        elif package_prefix and re.match(rf"\s*at {re.escape(package_prefix)}", line):
            hits.append(line.strip())
    return hits


def boot(n, plugin_name, package_prefix, commands):
    cursor = now_cursor()
    _api("POST", "/api/server/start")
    if not wait_for(lambda: DONE_LINE.search(logs_since(cursor)) is not None, 300, f"boot {n} Done"):
        record(f"boot-{n}", False, "server did not reach Done within 300s")
    time.sleep(3)  # let late startup lines land
    log = logs_since(cursor)

    enabling = re.search(rf"Enabling {re.escape(plugin_name)} v(\S+)", log)
    if not enabling:
        record(f"boot-{n}", False, f"no 'Enabling {plugin_name}' line during startup")
    version = enabling.group(1)
    RESULT["version"] = version

    for marker in (
        f"Error occurred while enabling {plugin_name}",
        f"Could not load 'plugins/{os.path.basename(CANDIDATE_JAR)}'",
        "UnknownDependencyException",
        f"Disabling {plugin_name}",
    ):
        if marker in log:
            record(f"boot-{n}", False, f"startup log contains {marker!r}")

    errors = attributable_errors(log, plugin_name, package_prefix)
    if errors:
        record(f"boot-{n}", False, "; ".join(errors[:5]))
    record(f"boot-{n}", True, f"enabled v{version}")

    if n == 1 and EXPECTED_VERSION:
        record("version", version == EXPECTED_VERSION, f"expected {EXPECTED_VERSION}, got {version}")

    cursor = now_cursor()
    send_command("plugins")
    listed = wait_for(lambda: plugin_name in logs_since(cursor), 30, "plugins output", poll=3)
    record(f"plugins-{n}", listed, "" if listed else f"'plugins' output does not mention {plugin_name}")

    help_commands(n, commands)


def help_commands(n, commands):
    """Every command the candidate declares must be known to the server's help map."""
    if not commands:
        record(f"commands-{n}", True, "no commands declared")
        return
    problems = []
    for cmd in commands:
        cursor = now_cursor()
        send_command(f"help {cmd}")
        answered = wait_for(lambda: HELP_ANSWERED.search(logs_since(cursor)) is not None, 20, f"help {cmd}", poll=2)
        missing = HELP_MISSING.search(logs_since(cursor))
        if missing:
            problems.append(f"help {cmd}: {missing.group(0)}")
        elif not answered:
            problems.append(f"help {cmd}: no help output within 20s")
    record(
        f"commands-{n}",
        not problems,
        f"{len(commands)} command(s): {commands}" if not problems else "; ".join(problems),
    )


def stop(n, plugin_name, package_prefix):
    cursor = now_cursor()
    _api("POST", "/api/server/stop")
    stopped = wait_for(lambda: not is_running(), 120, f"stop {n}")
    if not stopped:
        record(f"stop-{n}", False, "server still running 120s after stop")
    time.sleep(2)
    log = logs_since(cursor)
    errors = attributable_errors(log, plugin_name, package_prefix)
    if errors:
        record(f"stop-{n}", False, "; ".join(errors[:5]))
    record(f"stop-{n}", True, "clean stop")


# --- main --------------------------------------------------------------------------------

def main():
    print("=== Boot gate ===\n")

    meta = read_plugin_yml(CANDIDATE_JAR)
    plugin_name = meta.get("name")
    main_class = meta.get("main") or ""
    package_prefix = main_class.rsplit(".", 1)[0] if "." in main_class else main_class
    commands = list((meta.get("commands") or {}).keys())
    RESULT["plugin"] = plugin_name
    print(f"candidate: {plugin_name} main={main_class} declared-version={meta.get('version')} commands={commands}")

    supplied = {}
    for dep in DEPENDENCY_JARS:
        supplied[read_plugin_yml(dep).get("name")] = dep
    missing = [d for d in (meta.get("depend") or []) if d not in supplied]
    record(
        "dependencies",
        not missing,
        f"depend: {meta.get('depend') or []}; supplied: {sorted(supplied)}"
        + (f"; MISSING: {missing}" if missing else ""),
    )

    print("\n[baseline] waiting for the server's first start...")
    if not wait_for(is_running, 600, "baseline server", poll=10):
        record("baseline", False, "server never started (BuildTools / image problem, not the candidate)")
    if not wait_for(lambda: DONE_LINE.search(logs_since(datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))) is not None, 300, "baseline Done", poll=10):
        record("baseline", False, "baseline boot never reached Done")

    print("\n[deploy] dependencies then candidate...")
    for dep in DEPENDENCY_JARS:
        deploy_jar(dep)
    deploy_jar(CANDIDATE_JAR)

    print("\n[stop baseline]")
    _api("POST", "/api/server/stop")
    if not wait_for(lambda: not is_running(), 120, "baseline stop"):
        record("baseline", False, "baseline server did not stop")

    for n in (1, 2):
        print(f"\n[boot {n}]")
        boot(n, plugin_name, package_prefix, commands)
        print(f"\n[stop {n}]")
        stop(n, plugin_name, package_prefix)

    finish(True)


if __name__ == "__main__":
    main()
