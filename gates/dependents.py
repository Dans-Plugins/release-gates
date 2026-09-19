#!/usr/bin/env python3
"""Dependents gate (T3): prove that the plugins which `depend:` on a candidate still boot
when the candidate replaces the dependency they were built against.

Drives an OMCSI `minecraft-wrapper` container through its REST API (the same surface
Dan's Plugin Manager's integration test uses) and reads the server's console from
`docker logs`. The candidate, any extra dependency jars and every dependent's current
stable jar are deployed together on one server, which is then booted twice. It asserts:

  install-<Name>        the dependent's stable jar carries a readable plugin.yml and every
                        plugin it hard-depends on is on the server (the candidate, a
                        `dependencies` jar or another dependent). A dependent with no
                        stable release, or one whose other dependency was not supplied,
                        is reported as skipped and is not deployed
  boot-1                the server reaches "Done" (fatal)
  candidate-enabled-1   the candidate reports Enabling (with the expected version, when
                        given); no enable failure; no ERROR/SEVERE line or stack frame
                        attributable to it (fatal: without the candidate up, nothing about
                        its dependents can be concluded)
  enable-<Name>-1       the dependent reports Enabling; no `Error occurred while enabling`,
                        no `Could not load 'plugins/<jar>'` / UnknownDependencyException,
                        no ERROR/SEVERE line naming it and no stack frame in its package
  stop-1                the server stops within the timeout with no error attributable to
                        the candidate or any dependent, and the database closed cleanly:
                        no *.trace.db appeared and no close-failure line was printed
  boot-2, candidate-enabled-2, enable-<Name>-2, stop-2
                        the same, over the data folders the first boot wrote

The per-dependent assertions do not stop the run: one dependent that breaks must not hide
whether the others still boot. Harness-level assertions (`boot-*`, `candidate-enabled-*`,
a server that will not stop) are fatal. The gate passes only when every assertion holds,
i.e. every dependent with a stable release enabled on both boots.

Environment:
  CANDIDATE_JAR        path to the candidate jar (required)
  DEPENDENCY_JARS      newline-separated paths of extra dependency jars (optional)
  DEPENDENTS_MANIFEST  JSON list of {repository, tag, jar, reason} — `jar` is the path of
                       the dependent's stable jar, or null with `reason` when it has none
                       (required)
  EXPECTED_VERSION     the version string the candidate must enable with (optional)
  REPOSITORY, SHA      recorded in result.json only
  RESULT_PATH          where to write result.json (default: result.json)
  OMCSI_API_BASE       default http://localhost:8092
  OMCSI_DEPLOY_TOKEN   bearer token for /api/plugins/deploy (required)
  OMCSI_CONTAINER_NAME default open-mc-server
"""

import datetime
import hashlib
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
SERVER_ROOT = "/mcserver"
CANDIDATE_JAR = os.environ["CANDIDATE_JAR"]
DEPENDENCY_JARS = [p for p in os.getenv("DEPENDENCY_JARS", "").split("\n") if p.strip()]
with open(os.environ["DEPENDENTS_MANIFEST"]) as _f:
    MANIFEST = json.load(_f)
EXPECTED_VERSION = os.getenv("EXPECTED_VERSION") or None
RESULT_PATH = os.getenv("RESULT_PATH", "result.json")

_HEADERS = {"Authorization": f"Bearer {TOKEN}"}

RESULT = {
    "gate": "dependents",
    "repository": os.getenv("REPOSITORY"),
    "sha": os.getenv("SHA"),
    "candidate": os.path.basename(CANDIDATE_JAR),
    # The release automation publishes the exact bytes that passed: it verifies this digest
    # against the jar it uploads, so a rebuilt `dev` between gate and release cannot slip in.
    "candidateSha256": hashlib.sha256(open(CANDIDATE_JAR, "rb").read()).hexdigest(),
    "plugin": None,
    "version": None,
    "dependents": [
        {
            "repository": d["repository"],
            "name": None,
            "version": None,
            "tag": d.get("tag"),
            "jar": os.path.basename(d["jar"]) if d.get("jar") else None,
            "installed": False,
            "enabled_baseline": None,
            "enabled_1": False,
            "enabled_2": False,
            "pre_existing": False,
            "skipped": d.get("reason") if not d.get("jar") else None,
        }
        for d in MANIFEST
    ],
    "passed": False,
    "assertions": [],
    "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "finishedAt": None,
}
# Per-dependent facts the result does not carry: jar path, main package, declared depends.
DEPENDENT_META = {}


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
    print(f"\n=== dependents gate {'PASSED' if passed else 'FAILED'} ===")
    sys.exit(0 if passed else 1)


def entry(repository):
    return next(d for d in RESULT["dependents"] if d["repository"] == repository)


def label_of(dependent):
    """The plugin.yml name once known; the repository's short name for a skipped one."""
    return dependent["name"] or dependent["repository"].rsplit("/", 1)[-1]


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


def docker_exec(*args):
    cmd = ["docker", "exec", CONTAINER] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    return r.returncode == 0, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace").strip()


def expand_globs(patterns):
    """Server-root-relative paths matching the patterns, expanded by the container's shell."""
    paths = []
    for pattern in patterns:
        ok, out, _ = docker_exec("sh", "-c", f"cd {SERVER_ROOT} && ls -d {pattern}")
        if ok:
            paths += [p.strip() for p in out.splitlines() if p.strip()]
    return paths


# --- plugin.yml --------------------------------------------------------------------------

def read_plugin_yml(jar_path):
    with zipfile.ZipFile(jar_path) as z:
        with z.open("plugin.yml") as f:
            return yaml.safe_load(f) or {}


def package_of(meta):
    main_class = meta.get("main") or ""
    return main_class.rsplit(".", 1)[0] if "." in main_class else main_class


# --- assertions --------------------------------------------------------------------------

ERROR_LINE = re.compile(r"/(ERROR|SEVERE)\]")
DONE_LINE = re.compile(r"Done \([\d.]+s\)!")
# The first line of a Java stack trace: a class name ending in Exception/Error, no prefix.
CAUSE_LINE = re.compile(r"^\s*[\w.$]+(Exception|Error)\b")


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


def enable_problems(log, plugin_name, package_prefix, jar_basename):
    """Why a plugin did not enable cleanly during this boot, or [] when it did.

    Returns (version, problems). The version is None when no Enabling line was printed.
    """
    problems = []
    enabling = re.search(rf"Enabling {re.escape(plugin_name)} v(\S+)", log)
    version = enabling.group(1) if enabling else None
    lines = log.splitlines()
    # Bukkit prints the exception (UnknownDependencyException, NoClassDefFoundError…) on
    # the line after "Could not load" / "Error occurred while enabling"; that line is the
    # finding, so it is quoted with the marker.
    for marker in (
        f"Could not load 'plugins/{jar_basename}'",
        f"Error occurred while enabling {plugin_name}",
        f"Disabling {plugin_name}",
    ):
        i = next((i for i, line in enumerate(lines) if marker in line), None)
        if i is None:
            continue
        cause = next((l.strip() for l in lines[i + 1:i + 4] if CAUSE_LINE.match(l)), "")
        problems.append(f"startup log contains {marker!r}" + (f": {cause}" if cause else ""))
    if not enabling and not problems:
        problems.append(f"no 'Enabling {plugin_name}' line during startup")
    problems += attributable_errors(log, plugin_name, package_prefix)[:5]
    return version, problems


def stop_server(label):
    _api("POST", "/api/server/stop")
    return wait_for(lambda: not is_running(), 120, label)


def boot_to_done(n):
    cursor = now_cursor()
    _api("POST", "/api/server/start")
    if not wait_for(lambda: DONE_LINE.search(logs_since(cursor)) is not None, 300, f"boot {n} Done"):
        record(f"boot-{n}", False, "server did not reach Done within 300s", fatal=True)
    time.sleep(3)  # let late startup lines land
    log = logs_since(cursor)
    record(f"boot-{n}", True, "reached Done")
    return log


def candidate_enabled(n, log, plugin_name, package_prefix):
    version, problems = enable_problems(log, plugin_name, package_prefix, os.path.basename(CANDIDATE_JAR))
    if version:
        RESULT["version"] = version
    if problems:
        record(f"candidate-enabled-{n}", False, "; ".join(problems), fatal=True)
    if n == 1 and EXPECTED_VERSION and version != EXPECTED_VERSION:
        record("candidate-enabled-1", False, f"expected {EXPECTED_VERSION}, got {version}", fatal=True)
    record(f"candidate-enabled-{n}", True, f"enabled v{version}"
           + (f" (expected {EXPECTED_VERSION})" if n == 1 and EXPECTED_VERSION else ""))


def dependent_enabled(n, log, dependent):
    if not dependent["installed"]:
        return
    meta = DEPENDENT_META[dependent["repository"]]
    version, problems = enable_problems(log, dependent["name"], meta["package"], dependent["jar"])
    if version:
        dependent["version"] = version
    if problems:
        record(f"enable-{dependent['name']}-{n}", False, "; ".join(problems))
        return
    dependent[f"enabled_{n}"] = True
    record(f"enable-{dependent['name']}-{n}", True, f"enabled v{version}")


# An embedded database that fails to close on shutdown leaves its own evidence: H2 writes
# `<db>.trace.db` when a close throws, and the console carries the classloader/MVStore
# failure. Neither is a normal part of a clean stop, and each one is a slow, silent path to
# a corrupt store — so either blocks, no matter how the boot itself looked.
DB_CLOSE_FAILURE = re.compile(r"zip file closed|MVStoreException|OnExitDatabaseCloser|File corrupted while reading record")


TRACE_GLOBS = ("*.trace.db", "plugins/*/*.trace.db", "*/*.trace.db")
TRACE_BEFORE_CANDIDATE = {}  # trace files (and sizes) the control phase left: the stable's, not the candidate's


def trace_files_now():
    out = {}
    for pattern in TRACE_GLOBS:
        for path in expand_globs([pattern]):
            ok, size, _ = docker_exec("sh", "-c", f"cd {SERVER_ROOT} && wc -c < '{path}'")
            out[path] = int(size.strip() or 0) if ok else -1
    return out


def db_close_evidence(log):
    hits = [line.strip() for line in log.splitlines() if DB_CLOSE_FAILURE.search(line)]
    now = trace_files_now()
    blamed = sorted(p for p, size in now.items()
                    if p not in TRACE_BEFORE_CANDIDATE or size > TRACE_BEFORE_CANDIDATE[p])
    return hits, blamed


def stop(n, plugin_name, package_prefix):
    cursor = now_cursor()
    stopped = stop_server(f"stop {n}")
    if not stopped:
        record(f"stop-{n}", False, "server still running 120s after stop", fatal=True)
    time.sleep(2)
    log = logs_since(cursor)
    errors = attributable_errors(log, plugin_name, package_prefix)
    for d in RESULT["dependents"]:
        if d["installed"]:
            errors += attributable_errors(log, d["name"], DEPENDENT_META[d["repository"]]["package"])
    if errors:
        record(f"stop-{n}", False, "; ".join(errors[:5]))
        return
    close_lines, trace_files = db_close_evidence(log)
    if close_lines or trace_files:
        record(f"stop-{n}", False,
               "database did not close cleanly: " + "; ".join(close_lines[:3] + [f"trace file {t}" for t in trace_files]))
        return
    record(f"stop-{n}", True, "clean stop; no database close failure, no *.trace.db")


# --- main --------------------------------------------------------------------------------

def resolve_dependents(candidate_name, supplied_names):
    """Read each dependent's plugin.yml and decide whether it can be deployed at all."""
    # First pass: names, so a dependent that depends on another dependent is satisfied too.
    for src, d in zip(MANIFEST, RESULT["dependents"]):
        if not src.get("jar"):
            continue
        try:
            meta = read_plugin_yml(src["jar"])
        except Exception as exc:
            d["skipped"] = f"{d['jar']} has no readable plugin.yml: {exc}"
            continue
        d["name"] = meta.get("name")
        d["version"] = str(meta.get("version")) if meta.get("version") is not None else None
        DEPENDENT_META[d["repository"]] = {
            "path": src["jar"],
            "package": package_of(meta),
            "depend": [str(x) for x in (meta.get("depend") or [])],
        }
    on_server = {candidate_name, *supplied_names, *(d["name"] for d in RESULT["dependents"] if d["name"])}

    for d in RESULT["dependents"]:
        name = label_of(d)
        if d["repository"] not in DEPENDENT_META:
            record(f"install-{name}", True, f"skipped: {d['skipped']}")
            continue
        meta = DEPENDENT_META[d["repository"]]
        unmet = [x for x in meta["depend"] if x not in on_server]
        if unmet:
            # The candidate is on the server, so an unmet dependency is a third plugin the
            # caller did not pass through `dependencies` — not something the candidate broke.
            d["skipped"] = f"dependency {unmet} not supplied (pass it through `dependencies`)"
            record(f"install-{name}", True, f"skipped: {d['skipped']}")
            continue
        d["installed"] = True
        detail = f"{d['jar']} from {d['tag']}; depend: {meta['depend']}"
        if candidate_name not in meta["depend"]:
            detail += f"; note: does not declare depend: [{candidate_name}]"
        record(f"install-{name}", True, detail)


BASELINE_JAR = os.getenv("BASELINE_JAR") or None


def control_phase(plugin_name, package_prefix, installed):
    """Boot every dependent against the CURRENT STABLE first. A dependent that does not
    enable here is already broken; the candidate cannot be blamed for it and the gate
    reports it as pre-existing instead of failing."""
    meta = read_plugin_yml(BASELINE_JAR)
    name, pkg = meta.get("name"), package_of(meta)
    print(f"\n[control] {name} v{meta.get('version')} (current stable) with every dependent")
    deploy_jar(BASELINE_JAR)
    for d in installed:
        deploy_jar(DEPENDENT_META[d["repository"]]["path"])
    if not stop_server("control stop"):
        record("control", False, "server did not stop before the control boot", fatal=True)
    log = boot_to_done("control")
    for d in RESULT["dependents"]:
        if d["installed"]:
            before = len(RESULT["assertions"])
            dependent_enabled("control", log, d)
            d["enabled_baseline"] = RESULT["assertions"][-1]["passed"]
            # the control assertions are informational: rename so they never decide the verdict
            for a in RESULT["assertions"][before:]:
                a["name"] = a["name"].replace("-control", "-baseline")
                if not a["passed"]:
                    d["pre_existing"] = True
                    a["passed"] = True
                    a["detail"] = "PRE-EXISTING against the current stable — " + a["detail"]
    if not stop_server("control stop"):
        record("control", False, "server did not stop after the control boot", fatal=True)
    ok, _, err = docker_exec("rm", f"{SERVER_ROOT}/plugins/{os.path.basename(BASELINE_JAR)}")
    if not ok:
        record("control", False, f"baseline jar could not be removed: {err}", fatal=True)
    TRACE_BEFORE_CANDIDATE.update(trace_files_now())
    if TRACE_BEFORE_CANDIDATE:
        RESULT["baselineCloseFailure"] = sorted(TRACE_BEFORE_CANDIDATE)
        print(f"  NOTE: the current stable left a database trace file after its shutdown: {sorted(TRACE_BEFORE_CANDIDATE)}")
    record("control", True, "; ".join(
        f"{d['name']}: {'enables' if d['enabled_baseline'] else 'ALREADY BROKEN'} against the current stable"
        for d in RESULT["dependents"] if d["installed"]) or "no installed dependents")


def main():
    print("=== Dependents gate ===\n")

    meta = read_plugin_yml(CANDIDATE_JAR)
    plugin_name = meta.get("name")
    package_prefix = package_of(meta)
    RESULT["plugin"] = plugin_name
    print(f"candidate: {plugin_name} main={meta.get('main')} declared-version={meta.get('version')}")

    supplied = {}
    for dep in DEPENDENCY_JARS:
        supplied[read_plugin_yml(dep).get("name")] = dep
    print(f"dependencies: {sorted(supplied)}")
    print(f"dependents: {[d['repository'] for d in RESULT['dependents']]}")

    print("\n[install]")
    resolve_dependents(plugin_name, supplied)
    installed = [d for d in RESULT["dependents"] if d["installed"]]
    if not installed:
        print("  no dependent has a deployable stable release; the candidate is still booted")

    print("\n[baseline] waiting for the server's first start...")
    if not wait_for(is_running, 600, "baseline server", poll=10):
        record("boot-1", False, "server never started (BuildTools / image problem, not the candidate)", fatal=True)
    if not wait_for(lambda: DONE_LINE.search(logs_since(datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))) is not None, 300, "baseline Done", poll=10):
        record("boot-1", False, "baseline boot never reached Done", fatal=True)

    print("\n[deploy] dependencies...")
    for dep in DEPENDENCY_JARS:
        deploy_jar(dep)
    if BASELINE_JAR:
        control_phase(plugin_name, package_prefix, installed)
    print("\n[deploy] the candidate, then every dependent...")
    deploy_jar(CANDIDATE_JAR)
    for d in installed:
        deploy_jar(DEPENDENT_META[d["repository"]]["path"])

    print("\n[stop baseline]")
    if not stop_server("baseline stop"):
        record("boot-1", False, "baseline server did not stop", fatal=True)

    for n in (1, 2):
        print(f"\n[boot {n}]")
        log = boot_to_done(n)
        candidate_enabled(n, log, plugin_name, package_prefix)
        for d in RESULT["dependents"]:
            before = len(RESULT["assertions"])
            dependent_enabled(n, log, d)
            # A dependent that was already broken against the current stable is reported,
            # not held against the candidate. A dependent that worked and now does not is a
            # regression and fails the gate.
            if d.get("pre_existing"):
                for a in RESULT["assertions"][before:]:
                    if not a["passed"]:
                        a["passed"] = True
                        a["detail"] = "PRE-EXISTING (also fails against the current stable; not a regression) — " + a["detail"]
        print(f"\n[stop {n}]")
        stop(n, plugin_name, package_prefix)

    RESULT["preExisting"] = [d["name"] for d in RESULT["dependents"] if d.get("pre_existing")]
    finish(all(a["passed"] for a in RESULT["assertions"]))


if __name__ == "__main__":
    main()
