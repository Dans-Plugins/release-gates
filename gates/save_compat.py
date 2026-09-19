#!/usr/bin/env python3
"""Save-compatibility gate (T2-lite): prove a candidate plugin jar loads the data the
current stable release wrote, migrates it, and keeps it across its own restart.

Drives an OMCSI `minecraft-wrapper` container through its REST API (the same surface
Dan's Plugin Manager's integration test uses) and reads the server's console from
`docker logs`. The baseline (the current stable jar) is booted first and asked to write
data; that data is captured as a fixture; the candidate is then booted over it twice.
With a supplied fixture (FIXTURE_ARCHIVE, a plugin-fixtures release asset) nothing is
recorded: the archive is unpacked into the server root before the baseline's first
boot, the baseline is booted over it — which proves the fixture loads on the stable and
establishes the reference counts — and the candidate then follows exactly as below.
It asserts, in order:

  fixture-unpack     (supplied fixture only) the server is stopped after its first start
                     and the archive is unpacked into the server root as the server's own
                     user; every path the manifest lists exists
  baseline-boot      dependency jars + the baseline jar are deployed; after a restart the
                     baseline reports Enabling with no error attributable to it
  config-overrides   `dotted.key: value` lines are applied to plugins/<Name>/config.yml and
                     the baseline enables again over the edited config ("none" when empty)
  scenario           console commands are sent ~3s apart; once the console has been quiet
                     for 5s, no error is attributable to the baseline ("none" when empty)
  baseline-restart   the baseline enables over its own data; every `<n> <label> loaded`
                     line it prints is captured as the reference count for that label
  fixture-expected   (supplied fixture only) every label in the manifest's `expected` is
                     logged by the baseline with that count
  fixture            the server is stopped; plugins/<Name> and every extra data path are
                     copied out, listed (size, sha256) and archived; at least one file
  candidate-boot-1   the baseline jar is removed and the candidate deployed; the candidate
                     reports Enabling (with the expected version, when given); no enable
                     failure, no `Could not load` / UnknownDependencyException, no error
                     attributable to it
  counts-1           every label the baseline logged is logged by the candidate with the
                     same count; labels present on one side only are reported, not failed.
                     With a supplied fixture, also every label in the manifest's
                     `expected` with that count
  files-kept-1       every file in the fixture still exists — at its path, or moved into
                     plugins/<Name>/ by a migration; a removed file fails. Added files are
                     reported
  stop-1             the server stops within the timeout with no error attributable to
                     the candidate
  candidate-boot-2, counts-2, files-kept-2, stop-2
                     the same, over the data the candidate itself wrote

It stops at the first assertion that fails, writes `result.json`, and exits non-zero.

Environment:
  BASELINE_JAR         path to the current stable jar (required)
  CANDIDATE_JAR        path to the candidate jar (required)
  DEPENDENCY_JARS      newline-separated paths of dependency jars (optional)
  EXPECTED_VERSION     the version string the candidate must enable with (optional)
  CONFIG_OVERRIDES     newline-separated `dotted.key: value` lines (optional)
  SCENARIO             newline-separated console commands (optional)
  EXTRA_DATA_PATHS     newline-separated server-root-relative glob patterns (optional)
  FIXTURE_ARCHIVE      path to a fixture tar.gz to boot the baseline over instead of
                       recording one (optional); entries are server-root-relative, a
                       single wrapper directory is stripped
  FIXTURE_MANIFEST     path to that fixture's manifest.json (required with FIXTURE_ARCHIVE)
  FIXTURE_URL, FIXTURE_MANIFEST_URL
                       where they came from — recorded in result.json only
  WORK_DIR             where fixture/ and evidence/ are written (default: work)
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
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

import requests
import yaml

API_BASE = os.getenv("OMCSI_API_BASE", "http://localhost:8092")
TOKEN = os.environ["OMCSI_DEPLOY_TOKEN"]
CONTAINER = os.getenv("OMCSI_CONTAINER_NAME", "open-mc-server")
SERVER_ROOT = "/mcserver"
BASELINE_JAR = os.environ["BASELINE_JAR"]
CANDIDATE_JAR = os.environ["CANDIDATE_JAR"]
DEPENDENCY_JARS = [p for p in os.getenv("DEPENDENCY_JARS", "").split("\n") if p.strip()]
EXPECTED_VERSION = os.getenv("EXPECTED_VERSION") or None
CONFIG_OVERRIDES = [l.strip() for l in os.getenv("CONFIG_OVERRIDES", "").split("\n") if l.strip()]
SCENARIO = [l.strip() for l in os.getenv("SCENARIO", "").split("\n") if l.strip()]
EXTRA_DATA_PATHS = [l.strip() for l in os.getenv("EXTRA_DATA_PATHS", "").split("\n") if l.strip()]
FIXTURE_ARCHIVE = os.getenv("FIXTURE_ARCHIVE") or None
FIXTURE_MANIFEST = os.getenv("FIXTURE_MANIFEST") or None
WORK_DIR = os.getenv("WORK_DIR", "work")
RESULT_PATH = os.getenv("RESULT_PATH", "result.json")

if FIXTURE_ARCHIVE and not FIXTURE_MANIFEST:
    sys.exit("FIXTURE_ARCHIVE is set but FIXTURE_MANIFEST is not: a supplied fixture needs its manifest")
MANIFEST = json.load(open(FIXTURE_MANIFEST)) if FIXTURE_MANIFEST else {}
# Count per `<label>` that the fixture's manifest says a reload must find.
EXPECTED = {str(k): int(v) for k, v in (MANIFEST.get("expected") or {}).items()}

_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
DATA_PATHS = []  # server-root-relative paths the fixture covers, once known

RESULT = {
    "gate": "save-compat",
    "repository": os.getenv("REPOSITORY"),
    "sha": os.getenv("SHA"),
    "baseline": os.path.basename(BASELINE_JAR),
    "candidate": os.path.basename(CANDIDATE_JAR),
    # The release automation publishes the exact bytes that passed: it verifies this digest
    # against the jar it uploads, so a rebuilt `dev` between gate and release cannot slip in.
    "candidateSha256": __import__("hashlib").sha256(open(CANDIDATE_JAR, "rb").read()).hexdigest(),
    "plugin": None,
    "baselineVersion": None,
    "version": None,
    "passed": False,
    "counts": {},
    # A supplied fixture: where it came from and what its manifest promises. None when the
    # gate recorded the fixture itself from the baseline's scenario.
    "fixture": {
        "url": os.getenv("FIXTURE_URL") or None,
        "manifestUrl": os.getenv("FIXTURE_MANIFEST_URL") or None,
        "sha256": hashlib.sha256(open(FIXTURE_ARCHIVE, "rb").read()).hexdigest(),
        "kind": MANIFEST.get("kind", "recorded"),
        "plugin": MANIFEST.get("plugin"),
        "version": MANIFEST.get("version"),
        "minecraft": MANIFEST.get("minecraft"),
        "paths": MANIFEST.get("paths"),
        "expected": EXPECTED,
    } if FIXTURE_ARCHIVE else None,
    "fixtureFiles": 0,
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
    if DATA_PATHS:
        # Whatever the candidate left behind is evidence, pass or fail.
        try:
            capture(os.path.join(WORK_DIR, "candidate-data"), DATA_PATHS)
            archive(os.path.join(WORK_DIR, "candidate-data"), os.path.join(WORK_DIR, "evidence", "candidate-data.tar.gz"))
        except Exception as exc:  # evidence only; never masks the verdict
            print(f"  candidate data not archived: {exc}")
    print(f"\n=== save-compat gate {'PASSED' if passed else 'FAILED'} ===")
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


def docker_exec(*args, input_bytes=None):
    cmd = ["docker", "exec"] + (["-i"] if input_bytes is not None else []) + [CONTAINER] + list(args)
    r = subprocess.run(cmd, capture_output=True, input=input_bytes, timeout=60)
    return r.returncode == 0, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace").strip()


def copy_out(container_path, dest):
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    r = subprocess.run(["docker", "cp", f"{CONTAINER}:{container_path}", dest],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def expand_globs(patterns):
    """Server-root-relative paths matching the patterns, expanded by the container's shell."""
    paths = []
    for pattern in patterns:
        ok, out, _ = docker_exec("sh", "-c", f"cd {SERVER_ROOT} && ls -d {pattern}")
        if ok:
            paths += [p.strip() for p in out.splitlines() if p.strip()]
        else:
            print(f"  {pattern}: no match")
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
# "3 factions loaded (5ms)" -> ("3", "factions"); the label is what the plugin calls the
# collection, so the same label on a later boot is the same collection.
COUNT_LINE = re.compile(r"(\d+) ([\w][\w \-]*?) loaded")


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


def loaded_counts(log):
    counts = {}
    for line in log.splitlines():
        m = COUNT_LINE.search(line)
        if m:
            counts[m.group(2).strip()] = int(m.group(1))
    return counts


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


def restart_and_enable(name, plugin_name, package_prefix, jar_basename):
    """Stop (unless already stopped), start, and assert the plugin enabled cleanly.

    Returns (startup log, version). Every boot in this gate is a full stop→start, so a
    plugin that only reads its data at enable time really re-reads it.
    """
    if is_running():
        if not stop_server(f"{name} stop"):
            record(name, False, "server did not stop within 120s")
    cursor = now_cursor()
    _api("POST", "/api/server/start")
    if not wait_for(lambda: DONE_LINE.search(logs_since(cursor)) is not None, 300, f"{name} Done"):
        record(name, False, "server did not reach Done within 300s")
    time.sleep(3)  # let late startup lines land
    log = logs_since(cursor)

    enabling = re.search(rf"Enabling {re.escape(plugin_name)} v(\S+)", log)
    if not enabling:
        record(name, False, f"no 'Enabling {plugin_name}' line during startup")
    version = enabling.group(1)

    for marker in (
        f"Error occurred while enabling {plugin_name}",
        f"Could not load 'plugins/{jar_basename}'",
        "UnknownDependencyException",
        f"Disabling {plugin_name}",
    ):
        if marker in log:
            record(name, False, f"startup log contains {marker!r}")

    errors = attributable_errors(log, plugin_name, package_prefix)
    if errors:
        record(name, False, "; ".join(errors[:5]))
    return log, version


def apply_config_overrides(plugin_name):
    if not CONFIG_OVERRIDES:
        record("config-overrides", True, "none")
        return
    container_path = f"{SERVER_ROOT}/plugins/{plugin_name}/config.yml"
    local = os.path.join(WORK_DIR, "config-overrides", "config.yml")
    ok, err = copy_out(container_path, local)
    if not ok:
        record("config-overrides", False, f"plugins/{plugin_name}/config.yml could not be read after the baseline boot: {err}")
    with open(local) as f:
        config = yaml.safe_load(f) or {}
    applied = []
    for line in CONFIG_OVERRIDES:
        if ":" not in line:
            record("config-overrides", False, f"not a `dotted.key: value` line: {line!r}")
        key, _, value = line.partition(":")
        keys = key.strip().split(".")
        node = config
        for k in keys[:-1]:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = yaml.safe_load(value.strip()) if value.strip() else None
        applied.append(f"{key.strip()}={node[keys[-1]]!r}")
    # Written back through the server's own user (docker exec runs as the image's USER),
    # so the file keeps its owner and the plugin can still save to it.
    dumped = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    ok, _, err = docker_exec("sh", "-c", f"cat > '{container_path}'", input_bytes=dumped.encode("utf-8"))
    if not ok:
        record("config-overrides", False, f"config.yml could not be written back: {err}")
    with open(local, "w") as f:
        f.write(dumped)
    print(f"  applied {applied}")
    return applied


def run_scenario(plugin_name, package_prefix):
    if not SCENARIO:
        record("scenario", True, "none")
        return
    cursor = now_cursor()
    for i, cmd in enumerate(SCENARIO):
        send_command(cmd)
        if i < len(SCENARIO) - 1:
            time.sleep(3)
    # Commands run asynchronously: wait until the console has been quiet for 5s.
    last_len, quiet_since, deadline = -1, time.time(), time.time() + 120
    while time.time() < deadline:
        time.sleep(1)
        n = len(logs_since(cursor))
        if n != last_len:
            last_len, quiet_since = n, time.time()
        elif time.time() - quiet_since >= 5:
            break
    log = logs_since(cursor)
    errors = attributable_errors(log, plugin_name, package_prefix)
    if errors:
        record("scenario", False, "; ".join(errors[:5]))
    console = [l.split("]: ", 1)[-1].strip() for l in log.splitlines()
               if "INFO]: " in l and "[minecraft-wrapper]" not in l]
    record("scenario", True, f"{len(SCENARIO)} command(s): {SCENARIO}"
           + (f"; console: {console[:8]}" if console else "; console printed nothing"))


def listing_of(root):
    files = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            files[os.path.relpath(p, root).replace(os.sep, "/")] = {"size": os.path.getsize(p), "sha256": digest}
    return files


def capture(dest, data_paths):
    """Copy every server-root-relative path in data_paths into dest; return its listing."""
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    captured = []
    for rel in data_paths:
        ok, err = copy_out(f"{SERVER_ROOT}/{rel}", os.path.join(dest, rel))
        if ok:
            captured.append(rel)
        else:
            print(f"  {rel}: not captured ({err})")
    return captured, listing_of(dest)


def archive(src_dir, tar_path):
    os.makedirs(os.path.dirname(tar_path), exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(src_dir, arcname=os.path.basename(src_dir))


def fixture_members():
    """Server-root-relative entry names in the supplied archive, and how many leading
    path components to strip: 1 when every entry sits under one wrapper directory that is
    not itself a data path (the gate's own `fixture.tar.gz` evidence is wrapped in
    `fixture/`; plugin-fixtures release archives are not wrapped)."""
    with tarfile.open(FIXTURE_ARCHIVE) as tar:
        members = tar.getmembers()
    names = []
    for m in members:
        name = m.name[2:] if m.name.startswith("./") else m.name
        if name in ("", "."):
            continue
        if name.startswith("/") or ".." in name.split("/") or m.issym() or m.islnk():
            record("fixture-unpack", False, f"archive entry {m.name!r} is not a plain server-root-relative path")
        names.append(name.rstrip("/") + "/" if m.isdir() else name)
    tops = {n.split("/", 1)[0] for n in names}
    data_tops = {p.strip("/").split("/", 1)[0] for p in (MANIFEST.get("paths") or [])} | {"plugins"}
    strip = 0
    if len(tops) == 1:
        top = next(iter(tops))
        if top not in data_tops and any(n.startswith(top + "/") for n in names):
            strip = 1
            names = [n[len(top) + 1:] for n in names if n.rstrip("/") != top]
    return names, strip


def unpack_fixture():
    """Stop the server and unpack the supplied archive into the server root, through the
    container's own tar so every file is owned by the server's user and stays writable."""
    if not stop_server("fixture-unpack stop"):
        record("fixture-unpack", False, "server did not stop within 120s")
    names, strip = fixture_members()
    with open(FIXTURE_ARCHIVE, "rb") as f:
        archive = f.read()
    ok, _, err = docker_exec("sh", "-c", f"cd {SERVER_ROOT} && tar xzf - --strip-components={strip}", input_bytes=archive)
    if not ok:
        record("fixture-unpack", False, f"archive could not be unpacked into the server root: {err}")
    missing = []
    for path in MANIFEST.get("paths") or []:
        ok, _, _ = docker_exec("sh", "-c", f"cd {SERVER_ROOT} && ls -d '{path.strip('/')}'")
        if not ok:
            missing.append(path)
    if missing:
        record("fixture-unpack", False, f"manifest paths not present after unpacking: {missing}; archive holds {names[:10]}")
    files = [n for n in names if not n.endswith("/")]
    record("fixture-unpack", True,
           f"{len(files)} entr{'y' if len(files) == 1 else 'ies'} from {os.path.basename(FIXTURE_ARCHIVE)} "
           f"({MANIFEST.get('plugin')} {MANIFEST.get('version')}, {MANIFEST.get('kind', 'recorded')})"
           + (f", wrapper directory stripped" if strip else "")
           + f"; manifest paths present: {MANIFEST.get('paths')}")


def against_expected(counts):
    """(mismatched {label: (expected, actual)}, labels expected but not logged)."""
    mismatched = {l: (EXPECTED[l], counts[l]) for l in EXPECTED if l in counts and counts[l] != EXPECTED[l]}
    missing = sorted(l for l in EXPECTED if l not in counts)
    return mismatched, missing


def check_fixture_expected(baseline_counts):
    if not FIXTURE_ARCHIVE:
        return
    if not EXPECTED:
        record("fixture-expected", True, "manifest lists no expected counts")
        return
    mismatched, missing = against_expected(baseline_counts)
    if mismatched or missing:
        record("fixture-expected", False,
               f"the baseline does not load what the manifest promises: mismatch (expected, baseline) {mismatched}; not logged {missing}")
    record("fixture-expected", True, f"baseline logged every expected count: {EXPECTED}")


def check_counts(n, baseline_counts, log):
    counts = loaded_counts(log)
    for label, value in counts.items():
        RESULT["counts"].setdefault(label, [None, None, None])[n] = value
    if not baseline_counts:
        record(f"counts-{n}", True, "baseline logged no counts"
               + (f"; candidate logged {counts}" if counts else ""))
        return
    mismatched = {l: (baseline_counts[l], counts[l]) for l in baseline_counts if l in counts and counts[l] != baseline_counts[l]}
    only_baseline = sorted(set(baseline_counts) - set(counts))
    only_candidate = sorted(set(counts) - set(baseline_counts))
    matched = {l: counts[l] for l in baseline_counts if l in counts and l not in mismatched}
    detail = f"matched {matched}"
    if only_baseline:
        detail += f"; only baseline logged {only_baseline}"
    if only_candidate:
        detail += f"; only candidate logged {only_candidate}"
    if mismatched:
        record(f"counts-{n}", False, "mismatch (baseline, candidate): " + str(mismatched) + "; " + detail)
    if EXPECTED:
        exp_mismatched, exp_missing = against_expected(counts)
        if exp_mismatched or exp_missing:
            record(f"counts-{n}", False, "mismatch against the manifest (expected, candidate): " + str(exp_mismatched)
                   + (f"; expected but not logged {exp_missing}" if exp_missing else "") + "; " + detail)
        detail += "; equals the manifest's expected counts"
    record(f"counts-{n}", True, detail)


def check_files_kept(n, fixture, data_paths, plugin_name):
    _, after = capture(os.path.join(WORK_DIR, f"after-{n}"), data_paths)
    data_folder = f"plugins/{plugin_name}/"
    # A file counts as migrated only if a byte-identical copy now sits in the plugin's own
    # folder (same name, same sha256). A same-named file with different content is not the
    # operator's data — that case is reported as missing.
    in_data_folder = {(p.rsplit("/", 1)[-1], meta["sha256"]) for p, meta in after.items() if p.startswith(data_folder)}
    missing, migrated, changed = [], [], []
    for path, meta in fixture.items():
        if path in after:
            if after[path]["sha256"] != meta["sha256"]:
                changed.append(path)
        elif not path.startswith(data_folder) and (path.rsplit("/", 1)[-1], meta["sha256"]) in in_data_folder:
            migrated.append(path)
        else:
            missing.append(path)
    added = sorted(set(after) - set(fixture))
    detail = f"{len(fixture) - len(missing)}/{len(fixture)} kept"
    if changed:
        detail += f"; changed {changed[:10]}"
    if migrated:
        detail += f"; migrated into {data_folder}: {migrated[:10]}"
    if added:
        detail += f"; added {added[:10]}"
    if missing:
        record(f"files-kept-{n}", False, f"missing {missing[:10]}; " + detail)
    record(f"files-kept-{n}", True, detail)


# An embedded database that fails to close on shutdown leaves its own evidence: H2 writes
# `<db>.trace.db` when a close throws, and the console carries the classloader/MVStore
# failure. Neither is a normal part of a clean stop, and each one is a slow, silent path to
# a corrupt store — so either blocks, no matter how the boot itself looked.
DB_CLOSE_FAILURE = re.compile(r"zip file closed|MVStoreException|OnExitDatabaseCloser|File corrupted while reading record")


TRACE_GLOBS = ("*.trace.db", "plugins/*/*.trace.db", "*/*.trace.db")
# Trace files and their sizes as they stood when the fixture was recorded — i.e. whatever
# the BASELINE left behind. Only a trace file that is new, or that grew, is the candidate's.
TRACE_BEFORE_CANDIDATE = {}


def trace_files_now():
    out = {}
    for pattern in TRACE_GLOBS:
        for path in expand_globs([pattern]):
            ok, size, _ = docker_exec("sh", "-c", f"cd {SERVER_ROOT} && wc -c < '{path}'")
            out[path] = int(size.strip() or 0) if ok else -1
    return out


def db_close_evidence(log):
    """Console lines and trace files that show THIS release failed to close its database.
    A trace file the baseline already left (same size) is not evidence against the candidate."""
    hits = [line.strip() for line in log.splitlines() if DB_CLOSE_FAILURE.search(line)]
    now = trace_files_now()
    blamed = sorted(p for p, size in now.items()
                    if p not in TRACE_BEFORE_CANDIDATE or size > TRACE_BEFORE_CANDIDATE[p])
    return hits, blamed


def stop(n, plugin_name, package_prefix):
    cursor = now_cursor()
    stopped = stop_server(f"stop {n}")
    if not stopped:
        record(f"stop-{n}", False, "server still running 120s after stop")
    time.sleep(2)
    log = logs_since(cursor)
    errors = attributable_errors(log, plugin_name, package_prefix)
    if errors:
        record(f"stop-{n}", False, "; ".join(errors[:5]))
    close_lines, trace_files = db_close_evidence(log)
    if trace_files:
        # Keep the evidence: the trace file names the H2 instance (shaded package) that failed.
        ev = os.path.join(WORK_DIR, "evidence", "trace-files")
        os.makedirs(ev, exist_ok=True)
        for t in trace_files:
            copy_out(f"{SERVER_ROOT}/{t}", os.path.join(ev, t.replace("/", "__")))
    if close_lines or trace_files:
        record(f"stop-{n}", False,
               "database did not close cleanly: " + "; ".join(close_lines[:3] + [f"trace file {t}" for t in trace_files]))
    note = "; baseline had left a trace file, unchanged by the candidate" if TRACE_BEFORE_CANDIDATE else ""
    record(f"stop-{n}", True, f"clean stop; no database close failure, no new or grown *.trace.db{note}")


# --- main --------------------------------------------------------------------------------

def main():
    print("=== Save-compatibility gate ===\n")

    baseline_meta = read_plugin_yml(BASELINE_JAR)
    candidate_meta = read_plugin_yml(CANDIDATE_JAR)
    baseline_name = baseline_meta.get("name")
    plugin_name = candidate_meta.get("name")
    baseline_pkg = package_of(baseline_meta)
    candidate_pkg = package_of(candidate_meta)
    RESULT["plugin"] = plugin_name
    if baseline_name != plugin_name:
        RESULT["baselinePlugin"] = baseline_name
    print(f"baseline:  {baseline_name} main={baseline_meta.get('main')} declared-version={baseline_meta.get('version')}")
    print(f"candidate: {plugin_name} main={candidate_meta.get('main')} declared-version={candidate_meta.get('version')}")
    print(f"overrides={CONFIG_OVERRIDES} scenario={SCENARIO} extra_data_paths={EXTRA_DATA_PATHS}")
    if FIXTURE_ARCHIVE:
        print(f"fixture:   {os.path.basename(FIXTURE_ARCHIVE)} sha256={RESULT['fixture']['sha256']} "
              f"{MANIFEST.get('plugin')} {MANIFEST.get('version')} ({MANIFEST.get('kind', 'recorded')}) expected={EXPECTED}")

    print("\n[baseline] waiting for the server's first start...")
    if not wait_for(is_running, 600, "baseline server", poll=10):
        record("baseline-boot", False, "server never started (BuildTools / image problem, not the plugin)")
    if not wait_for(lambda: DONE_LINE.search(logs_since(datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))) is not None, 300, "baseline Done", poll=10):
        record("baseline-boot", False, "baseline boot never reached Done")

    print("\n[deploy] dependencies then the baseline jar...")
    for dep in DEPENDENCY_JARS:
        deploy_jar(dep)
    deploy_jar(BASELINE_JAR)

    if FIXTURE_ARCHIVE:
        # The supplied fixture goes in before the baseline ever runs, so the baseline's first
        # boot is already a boot over that data — the proof that the fixture loads on the
        # current stable release.
        print("\n[fixture-unpack]")
        unpack_fixture()

    print("\n[baseline-boot]")
    log, baseline_version = restart_and_enable("baseline-boot", baseline_name, baseline_pkg, os.path.basename(BASELINE_JAR))
    RESULT["baselineVersion"] = baseline_version
    record("baseline-boot", True, f"{baseline_name} enabled v{baseline_version}"
           + (f" over the supplied fixture; counts {loaded_counts(log)}" if FIXTURE_ARCHIVE else ""))

    print("\n[config-overrides]")
    applied = apply_config_overrides(baseline_name)
    if applied:
        restart_and_enable("config-overrides", baseline_name, baseline_pkg, os.path.basename(BASELINE_JAR))
        record("config-overrides", True, f"applied {applied}; enabled again")

    print("\n[scenario]")
    run_scenario(baseline_name, baseline_pkg)

    print("\n[baseline-restart]")
    log, _ = restart_and_enable("baseline-restart", baseline_name, baseline_pkg, os.path.basename(BASELINE_JAR))
    baseline_counts = loaded_counts(log)
    for label, value in baseline_counts.items():
        RESULT["counts"][label] = [value, None, None]
    record("baseline-restart", True, f"enabled over its own data; counts {baseline_counts}")

    if FIXTURE_ARCHIVE:
        print("\n[fixture-expected]")
        check_fixture_expected(baseline_counts)

    print("\n[fixture]")
    if not stop_server("fixture stop"):
        record("fixture", False, "server did not stop within 120s")
    data_paths = [f"plugins/{baseline_name}"]
    if plugin_name != baseline_name:
        data_paths.append(f"plugins/{plugin_name}")
    data_paths += [p for p in expand_globs(EXTRA_DATA_PATHS) if p not in data_paths]
    DATA_PATHS.extend(data_paths)
    # Anything the baseline left behind on its own shutdowns is a fact about the current
    # stable release, not about the candidate. Record it, and use it as the reference point
    # for the candidate's stop checks.
    TRACE_BEFORE_CANDIDATE.update(trace_files_now())
    if TRACE_BEFORE_CANDIDATE:
        RESULT["baselineCloseFailure"] = sorted(TRACE_BEFORE_CANDIDATE)
        print(f"  NOTE: the baseline ({baseline_name} v{RESULT.get('baselineVersion')}) left a database trace file after its own shutdown: {sorted(TRACE_BEFORE_CANDIDATE)} — the current stable release does not close its database cleanly")
    fixture_dir = os.path.join(WORK_DIR, "fixture")
    captured, fixture = capture(fixture_dir, data_paths)
    RESULT["fixtureFiles"] = len(fixture)
    os.makedirs(os.path.join(WORK_DIR, "evidence"), exist_ok=True)
    with open(os.path.join(WORK_DIR, "evidence", "fixture-listing.json"), "w") as f:
        json.dump({"paths": captured, "files": fixture}, f, indent=2)
    archive(fixture_dir, os.path.join(WORK_DIR, "evidence", "fixture.tar.gz"))
    if not fixture:
        record("fixture", False, f"nothing captured from {data_paths}")
    record("fixture", True, f"{len(fixture)} file(s) from {captured}"
           + ("; the supplied fixture as the baseline left it" if FIXTURE_ARCHIVE else ""))

    print("\n[candidate] swapping the baseline jar for the candidate...")
    ok, _, err = docker_exec("rm", f"{SERVER_ROOT}/plugins/{os.path.basename(BASELINE_JAR)}")
    if not ok:
        record("candidate-boot-1", False, f"baseline jar could not be removed: {err}")
    deploy_jar(CANDIDATE_JAR)

    for n in (1, 2):
        print(f"\n[candidate-boot-{n}]")
        log, version = restart_and_enable(f"candidate-boot-{n}", plugin_name, candidate_pkg, os.path.basename(CANDIDATE_JAR))
        RESULT["version"] = version
        if n == 1 and EXPECTED_VERSION and version != EXPECTED_VERSION:
            record("candidate-boot-1", False, f"expected {EXPECTED_VERSION}, got {version}")
        record(f"candidate-boot-{n}", True, f"enabled v{version}"
               + (f" (expected {EXPECTED_VERSION})" if n == 1 and EXPECTED_VERSION else ""))

        print(f"\n[counts-{n}]")
        check_counts(n, baseline_counts, log)

        print(f"\n[files-kept-{n}]")
        check_files_kept(n, fixture, data_paths, plugin_name)

        print(f"\n[stop-{n}]")
        stop(n, plugin_name, candidate_pkg)

    finish(True)


if __name__ == "__main__":
    main()
