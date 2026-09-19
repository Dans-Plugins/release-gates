#!/usr/bin/env python3
"""Install gate: prove `/dpm get <slug>` installs a plugin's current stable release on a
fresh server, and that the installed plugin enables.

Drives an OMCSI `minecraft-wrapper` container through its REST API (the same surface
Dan's Plugin Manager's integration test uses) and reads the server's console from
`docker logs`. It asserts, in order:

  dpm            the server reaches "Done" with Dan's Plugin Manager enabled
  get-<slug>     `dpm get <slug>` reports a download (or "already up to date"), and the jar
                 DPM wrote to the plugins folder carries a readable plugin.yml
  boot           the server reaches "Done" over the plugins DPM installed
  enable-<slug>  the plugin logs `Enabling <name> v<version>`; no enable failure; no
                 ERROR/SEVERE line or stack frame attributable to it
  stop           the server stops within the timeout with no error attributable to any of them

Unlike the boot gate, the per-plugin assertions (`get-*`, `enable-*`) do not stop the run:
one slug that DPM cannot find must not hide whether the other five install. Harness-level
assertions (`dpm`, `boot`) are fatal. The gate passes only when every assertion holds.

Environment:
  PLUGINS              comma-separated DPM slugs (required)
  DPM_JAR              path to the Dan's Plugin Manager jar to deploy (required)
  DPM_SOURCE           "input" when DPM_JAR was supplied by the caller, "latest" when it is
                       the /releases/latest jar (default: latest); recorded only
  JARS_DIR             where installed jars are copied out of the container (default: work/jars)
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
PLUGINS = [s.strip() for s in os.environ["PLUGINS"].split(",") if s.strip()]
DPM_JAR = os.environ["DPM_JAR"]
DPM_SOURCE = os.getenv("DPM_SOURCE") or "latest"
JARS_DIR = os.getenv("JARS_DIR", "work/jars")
RESULT_PATH = os.getenv("RESULT_PATH", "result.json")

_HEADERS = {"Authorization": f"Bearer {TOKEN}"}

RESULT = {
    "gate": "dpm-install",
    "dpm": os.path.basename(DPM_JAR),
    "dpmSource": DPM_SOURCE,
    "dpmVersion": None,
    "plugins": [
        {"slug": slug, "name": None, "version": None, "tag": None, "installed": False, "enabled": False}
        for slug in PLUGINS
    ],
    "passed": False,
    "assertions": [],
    "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "finishedAt": None,
}


# --- reporting ---------------------------------------------------------------------------

def record(name, passed, detail="", fatal=False):
    RESULT["assertions"].append({"name": name, "passed": passed, "detail": detail})
    print(f"  {'PASS' if passed else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))
    if not passed and fatal:
        finish(False)


def finish(passed):
    RESULT["passed"] = passed
    RESULT["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(RESULT_PATH, "w") as f:
        json.dump(RESULT, f, indent=2)
    print(f"\n=== install gate {'PASSED' if passed else 'FAILED'} ===")
    sys.exit(0 if passed else 1)


def entry(slug):
    return next(p for p in RESULT["plugins"] if p["slug"] == slug)


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


def copy_out(container_path, dest):
    r = subprocess.run(["docker", "cp", f"{CONTAINER}:{container_path}", dest],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


# --- plugin.yml --------------------------------------------------------------------------

def read_plugin_yml(jar_path):
    with zipfile.ZipFile(jar_path) as z:
        with z.open("plugin.yml") as f:
            return yaml.safe_load(f) or {}


# --- assertions --------------------------------------------------------------------------

ERROR_LINE = re.compile(r"/(ERROR|SEVERE)\]")
DONE_LINE = re.compile(r"Done \([\d.]+s\)!")

# What DPM 0.6.0's GetCommand prints when `dpm get <slug>` has finished, on either the
# single-plugin path or the batch path it takes when a hard dependency must come along.
GET_TERMINAL = (
    "Restart the server to enable",
    "already up to date",
    "Done:",
    "Plugin not found:",
    "has no published release",
    "Failed to download",
    "Could not reach GitHub",
    "Could not write",
    "Something went wrong",
    "GitHub rate limit reached",
)
GET_FAILURE = GET_TERMINAL[3:]


def stop_server(label):
    """Stop the server; a server that is already stopped (wrapper answers 409) counts as stopped."""
    if not is_running():
        return True
    try:
        _api("POST", "/api/server/stop")
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 409:
            raise
    return wait_for(lambda: not is_running(), 120, label)


def boot_to_done(name):
    """Start the server; return the startup log, or fail the gate."""
    cursor = now_cursor()
    _api("POST", "/api/server/start")
    if not wait_for(lambda: DONE_LINE.search(logs_since(cursor)) is not None, 300, f"{name} Done"):
        record(name, False, "server did not reach Done within 300s", fatal=True)
    time.sleep(3)  # let late startup lines land
    return logs_since(cursor)


def attributable_errors(log, plugin_name, package_prefix):
    """ERROR/SEVERE lines that name the plugin, plus stack frames inside its package.

    Console lines carry a `[time] [thread/LEVEL]:` prefix, so a frame is searched for
    anywhere in the line, whatever level it was printed at.
    """
    hits = []
    for line in log.splitlines():
        if ERROR_LINE.search(line) and (f"[{plugin_name}]" in line or plugin_name in line):
            hits.append(line.strip())
        elif package_prefix and re.search(rf"\sat {re.escape(package_prefix)}", line):
            hits.append(line.strip())
    return hits


def get(slug):
    p = entry(slug)
    cursor = now_cursor()
    send_command(f"dpm get {slug}")
    done = wait_for(
        lambda: any(m in logs_since(cursor) for m in GET_TERMINAL), 120, f"dpm get {slug}", poll=3
    )
    log = logs_since(cursor)
    if not done:
        record(f"get-{slug}", False, "DPM printed no outcome within 120s")
        return

    installed = re.search(rf"\[DPM\] Installed {re.escape(slug)}(?: (\S+))?\.", log)
    up_to_date = re.search(rf"\b{re.escape(slug)}\b[^\n]*already up to date", log)
    failures = [line.strip() for line in log.splitlines()
                if any(m in line for m in GET_FAILURE) and "[minecraft-wrapper]" not in line]
    if failures and not (installed or up_to_date):
        record(f"get-{slug}", False, "; ".join(failures[:3]))
        return
    if not (installed or up_to_date):
        record(f"get-{slug}", False, "DPM finished without reporting a download for this slug")
        return
    if installed and installed.group(1):
        p["tag"] = installed.group(1)

    extra = [m.group(1) for m in re.finditer(r"Also downloading required dependency (\S+)\.", log)]

    os.makedirs(JARS_DIR, exist_ok=True)
    dest = os.path.join(JARS_DIR, f"{slug}.jar")
    ok, err = copy_out(f"/mcserver/plugins/{slug}.jar", dest)
    if not ok:
        record(f"get-{slug}", False, f"DPM reported success but /mcserver/plugins/{slug}.jar could not be read: {err}")
        return
    try:
        meta = read_plugin_yml(dest)
    except Exception as exc:  # not a jar, or no plugin.yml
        record(f"get-{slug}", False, f"{slug}.jar has no readable plugin.yml: {exc}")
        return
    p["name"] = meta.get("name")
    p["main"] = meta.get("main") or ""
    p["installed"] = True
    detail = f"{p['name']} {p['tag'] or ''} ({os.path.getsize(dest) // 1024} KB)".replace("  ", " ")
    if up_to_date and not installed:
        detail += "; already up to date"
    if extra:
        detail += f"; also fetched {extra}"
    record(f"get-{slug}", True, detail)


def enable(slug, log):
    p = entry(slug)
    if not p["installed"]:
        record(f"enable-{slug}", False, "skipped: not installed")
        return
    name = p["name"]
    enabling = re.search(rf"Enabling {re.escape(name)} v(\S+)", log)
    if not enabling:
        record(f"enable-{slug}", False, f"no 'Enabling {name}' line during startup")
        return
    p["version"] = enabling.group(1)

    for marker in (
        f"Error occurred while enabling {name}",
        f"Could not load 'plugins/{slug}.jar'",
        f"Disabling {name}",
    ):
        if marker in log:
            record(f"enable-{slug}", False, f"startup log contains {marker!r}")
            return
    if re.search(rf"UnknownDependencyException[^\n]*\n[^\n]*{re.escape(slug)}\.jar", log):
        record(f"enable-{slug}", False, "UnknownDependencyException while loading")
        return

    main_class = p.get("main") or ""
    package_prefix = main_class.rsplit(".", 1)[0] if "." in main_class else main_class
    errors = attributable_errors(log, name, package_prefix)
    if errors:
        record(f"enable-{slug}", False, "; ".join(errors[:5]))
        return
    p["enabled"] = True
    record(f"enable-{slug}", True, f"enabled v{p['version']}")


# --- main --------------------------------------------------------------------------------

def main():
    print("=== Install gate ===\n")
    print(f"plugins: {PLUGINS}")

    dpm_meta = read_plugin_yml(DPM_JAR)
    dpm_name = dpm_meta.get("name")
    print(f"manager: {dpm_name} declared-version={dpm_meta.get('version')}")

    print("\n[baseline] waiting for the server's first start...")
    if not wait_for(is_running, 600, "baseline server", poll=10):
        record("baseline", False, "server never started (BuildTools / image problem, not the plugins)", fatal=True)
    if not wait_for(lambda: DONE_LINE.search(logs_since(datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))) is not None, 300, "baseline Done", poll=10):
        record("baseline", False, "baseline boot never reached Done", fatal=True)

    print("\n[deploy] Dan's Plugin Manager...")
    deploy_jar(DPM_JAR)

    print("\n[stop baseline]")
    if not stop_server("baseline stop"):
        record("baseline", False, "baseline server did not stop", fatal=True)

    print("\n[boot with DPM]")
    log = boot_to_done("dpm")
    enabling = re.search(rf"Enabling {re.escape(dpm_name)} v(\S+)", log)
    if not enabling:
        record("dpm", False, f"no 'Enabling {dpm_name}' line during startup", fatal=True)
    if f"Error occurred while enabling {dpm_name}" in log:
        record("dpm", False, f"startup log contains 'Error occurred while enabling {dpm_name}'", fatal=True)
    RESULT["dpmVersion"] = enabling.group(1)
    record("dpm", True, f"{dpm_name} v{enabling.group(1)} enabled")

    print("\n[get]")
    for slug in PLUGINS:
        get(slug)

    print("\n[stop after get]")
    if not stop_server("stop after get"):
        record("boot", False, "server did not stop after dpm get", fatal=True)

    print("\n[boot with installed plugins]")
    log = boot_to_done("boot")
    record("boot", True, "reached Done")
    for slug in PLUGINS:
        enable(slug, log)

    print("\n[stop]")
    cursor = now_cursor()
    stopped = stop_server("stop")
    time.sleep(2)
    if not stopped:
        record("stop", False, "server still running 120s after stop")
    else:
        errors = []
        for p in RESULT["plugins"]:
            if p["installed"]:
                main_class = p.get("main") or ""
                prefix = main_class.rsplit(".", 1)[0] if "." in main_class else main_class
                errors += attributable_errors(logs_since(cursor), p["name"], prefix)
        record("stop", not errors, "clean stop" if not errors else "; ".join(errors[:5]))

    finish(all(a["passed"] for a in RESULT["assertions"]))


if __name__ == "__main__":
    main()
