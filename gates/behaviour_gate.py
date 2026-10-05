"""Behaviour gate (T4, Stephenson-Software RFC 0017): does the candidate behave like the stable
release for every row of a behaviour table?

A behaviour table (normally a file in Dans-Plugins/plugin-fixtures) is a matrix of rows: who (a
role) does what (an action with an item) to which target, where (an arena), under which config
group. The plugin-fixtures behaviour driver plays the table with mineflayer bots and writes one
outcome per row, read back from the world. This gate runs the driver on the baseline (the current
stable release) and on the candidate, each on fresh plugin data and once per config group, and
compares the outcomes. No expected values are needed: what the gate reports is what changed.

Rows the driver could not decide (its control did not change the world, the bot was not aiming or
not in place, the server stopped answering) are `not-checked` and never compared. A row that
changed is replayed once on both jars and only counts as changed when it changes again, so a
single flaky run cannot fail the gate.

Assertions (result.json, like every gate):

  baseline-boot / candidate-boot   each jar enables cleanly on fresh data for every config group
  behaviour-harness                both passes ran, and fewer than MAX_NOT_CHECKED of the rows of
                                   either pass are not-checked; otherwise the run gives no verdict
  behaviour-diff                   no row changed. Rows whose only difference is the refusal
                                   message's lang key are listed as message-changed and never
                                   fail it.

The verdict of `behaviour-diff` is the gate's verdict; quartermaster decides whether it blocks a
release (RFC 0017: advisory until the owner makes it required per plugin).

Environment:
  OMCSI_API_BASE, OMCSI_DEPLOY_TOKEN, OMCSI_CONTAINER_NAME   as for the other gates
  BASELINE_JAR, CANDIDATE_JAR      local paths
  DEPENDENCY_JARS                  newline-separated local paths, deployed beside both
  ROWS_URL, SETUP_URL, DRIVER_URL  raw URLs of the table, its setup module and the driver
  SCENARIO_NODE_DIR                directory where mineflayer is installed
  SCENARIO_MC_PORT, SCENARIO_RCON_PORT, RCON_PASSWORD
  WORK_DIR, RESULT_PATH, REPOSITORY, SHA
  MAX_NOT_CHECKED                  fraction, default 0.10
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

import usage_tags  # CI tags on every usage event the server sends (gates/usage_tags.py)

MODE = os.getenv("MODE", "single")  # single | plan | pass | compare (see main)
API_BASE = os.getenv("OMCSI_API_BASE", "").rstrip("/")
TOKEN = os.getenv("OMCSI_DEPLOY_TOKEN", "")
CONTAINER = os.getenv("OMCSI_CONTAINER_NAME", "open-mc-server")
SERVER_ROOT = "/mcserver"
BASELINE_JAR = os.getenv("BASELINE_JAR", "")
CANDIDATE_JAR = os.getenv("CANDIDATE_JAR", "")
DEPENDENCY_JARS = [p for p in os.getenv("DEPENDENCY_JARS", "").split("\n") if p.strip()]
ROWS_URL = os.environ["ROWS_URL"]
SETUP_URL = os.getenv("SETUP_URL", "")
DRIVER_URL = os.getenv("DRIVER_URL", "")
NODE_DIR = os.getenv("SCENARIO_NODE_DIR", "")
MC_PORT = os.getenv("SCENARIO_MC_PORT", "25565")
RCON_PORT = os.getenv("SCENARIO_RCON_PORT", "25575")
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")
WORK_DIR = os.getenv("WORK_DIR", "work")
RESULT_PATH = os.getenv("RESULT_PATH", "result.json")
MAX_NOT_CHECKED = float(os.getenv("MAX_NOT_CHECKED", "0.10"))
DRIVER_TIMEOUT = int(os.getenv("DRIVER_TIMEOUT", "1800"))
# Sharded runs (behaviour-gate.yml): rows per pass job, and the pass a `pass` job plays.
SHARD_SIZE = int(os.getenv("SHARD_SIZE", "15"))
SIDE = os.getenv("SIDE", "")          # stable | candidate
SHARD = os.getenv("SHARD", "")        # a key from shards()
PASS_DIR = os.getenv("PASS_DIR", "")  # where compare finds every pass job's pass.json

_HEADERS = {"Authorization": f"Bearer {TOKEN}"}

RESULT = {
    "gate": "behaviour",
    "repository": os.getenv("REPOSITORY"),
    "sha": os.getenv("SHA"),
    "baseline": os.path.basename(BASELINE_JAR),
    "candidate": os.path.basename(CANDIDATE_JAR),
    "candidateSha256": hashlib.sha256(open(CANDIDATE_JAR, "rb").read()).hexdigest() if CANDIDATE_JAR else None,
    "rowsUrl": ROWS_URL,
    "setupUrl": SETUP_URL,
    "driverUrl": DRIVER_URL,
    "plugin": None,
    "baselineVersion": None,
    "version": None,
    "passed": False,
    "rows": [],
    "assertions": [],
    "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "finishedAt": None,
}


# --- reporting ---------------------------------------------------------------------------

def record(name, passed, detail="", fatal=True):
    RESULT["assertions"].append({"name": name, "passed": passed, "detail": detail})
    print(f"  {'PASS' if passed else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))
    if not passed and fatal:
        finish(False)


def finish(passed):
    RESULT["passed"] = passed
    RESULT["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(RESULT_PATH, "w") as f:
        json.dump(RESULT, f, indent=2)
    print(f"\n=== behaviour gate {'PASSED' if passed else 'FAILED'} ===")
    sys.exit(0 if passed else 1)


# --- OMCSI (same calls as gates/save_compat.py) ---------------------------------------------

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
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)


def logs_since(cursor):
    r = subprocess.run(["docker", "logs", "--since", cursor.isoformat(), CONTAINER], capture_output=True, text=True, timeout=20)
    return r.stdout + r.stderr


def deploy_jar(path):
    name = os.path.basename(path)
    with open(path, "rb") as jar:
        _api("POST", "/api/plugins/deploy", data={"pluginName": name}, files={"file": (name, jar, "application/java-archive")})
    print(f"  deployed {name}")


def docker_exec(*args, input_bytes=None):
    cmd = ["docker", "exec"] + (["-i"] if input_bytes is not None else []) + [CONTAINER] + list(args)
    r = subprocess.run(cmd, capture_output=True, input=input_bytes, timeout=60)
    return r.returncode == 0, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace").strip()


def stop_server(label):
    if not is_running():
        return True
    try:
        _api("POST", "/api/server/stop")
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 409:
            raise
    return wait_for(lambda: not is_running(), 120, label)


def read_plugin_yml(jar_path):
    with zipfile.ZipFile(jar_path) as z:
        with z.open("plugin.yml") as f:
            return yaml.safe_load(f) or {}


def package_of(meta):
    main_class = meta.get("main") or ""
    return main_class.rsplit(".", 1)[0] if "." in main_class else main_class


ERROR_LINE = re.compile(r"/(ERROR|SEVERE)\]")
DONE_LINE = re.compile(r"Done \([\d.]+s\)!")


def attributable_errors(log, plugin_name, package_prefix):
    hits = []
    for line in log.splitlines():
        if ERROR_LINE.search(line) and (f"[{plugin_name}]" in line or plugin_name in line):
            hits.append(line.strip())
        elif package_prefix and re.search(rf"\sat {re.escape(package_prefix)}", line):
            hits.append(line.strip())
    return hits


def boot(name, plugin_name, package_prefix):
    """Stop, start, and require the plugin to enable without attributable errors. Returns its version."""
    if not stop_server(f"{name} stop"):
        record(name, False, "server did not stop within 120s")
    ok, detail = usage_tags.write_ci_trace_config(CONTAINER)
    if not ok:
        record(name, False, detail)
    cursor = now_cursor()
    _api("POST", "/api/server/start")
    if not wait_for(lambda: DONE_LINE.search(logs_since(cursor)) is not None, 300, f"{name} Done"):
        record(name, False, "server did not reach Done within 300s")
    time.sleep(3)
    log = logs_since(cursor)
    enabling = re.search(rf"Enabling {re.escape(plugin_name)} v(\S+)", log)
    if not enabling:
        record(name, False, f"no 'Enabling {plugin_name}' line during startup")
    errors = attributable_errors(log, plugin_name, package_prefix)
    if errors:
        record(name, False, "; ".join(errors[:5]))
    return enabling.group(1)


def wipe(data_paths):
    """Remove the plugin's data (server-root-relative globs from the table) so each run starts fresh."""
    for p in data_paths:
        docker_exec("sh", "-c", f"cd {SERVER_ROOT} && rm -rf {p}")


def write_config_overrides(plugin_name, overrides):
    """Apply {dotted.key: value} to plugins/<Name>/config.yml (as gates/save_compat.py does)."""
    container_path = f"{SERVER_ROOT}/plugins/{plugin_name}/config.yml"
    ok, out, err = docker_exec("cat", container_path)
    if not ok:
        record("config-overrides", False, f"plugins/{plugin_name}/config.yml could not be read: {err}")
    config = yaml.safe_load(out) or {}
    for key, value in overrides.items():
        keys = key.split(".")
        node = config
        for k in keys[:-1]:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
    dumped = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    ok, _, err = docker_exec("sh", "-c", f"cat > '{container_path}'", input_bytes=dumped.encode("utf-8"))
    if not ok:
        record("config-overrides", False, f"config.yml could not be written back: {err}")
    print(f"  applied {overrides}")


# --- the driver ----------------------------------------------------------------------------

def fetch(url, dest):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)
    return dest


def extract_lang(jar_path, dest):
    """The candidate's English lang file, so refusal messages are compared as lang keys."""
    with zipfile.ZipFile(jar_path) as z:
        names = [n for n in z.namelist() if n.endswith("lang_en_US.properties")]
        if not names:
            return None
        with z.open(names[0]) as src, open(dest, "wb") as out:
            out.write(src.read())
    return dest


def run_driver(files, group, label, only=None):
    out_json = os.path.join(WORK_DIR, "evidence", f"{label}-{group}.json")
    out_log = os.path.join(WORK_DIR, "evidence", f"{label}-{group}.log")
    cmd = ["node", files["driver"], "--rows", files["rows"], "--setup", files["setup"], "--group", group,
           "--host", "localhost", "--port", MC_PORT, "--rcon-port", RCON_PORT, "--rcon-password", RCON_PASSWORD,
           "--label", label, "--json-out", out_json]
    if files.get("lang"):
        cmd += ["--lang", files["lang"]]
    if only:
        cmd += ["--only", ",".join(only)]
    env = {**os.environ, "NODE_PATH": os.path.join(NODE_DIR, "node_modules")}
    with open(out_log, "w") as log:
        try:
            r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=DRIVER_TIMEOUT)
            code = r.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    if code != 0 or not os.path.exists(out_json):
        tail = open(out_log).read()[-600:]
        return None, f"driver exited {code}: {tail}"
    return json.load(open(out_json)), ""


def play(jar, label, table, files, plugin_name, package_prefix, deployed, only_by_group=None):
    """Run every config group (or only the given rows) on `jar`, each on fresh plugin data."""
    results = {}
    version = None
    for group, overrides in table["configGroups"].items():
        only = None
        if only_by_group is not None:
            only = only_by_group.get(group)
            if not only:
                continue
        print(f"\n[{label}] group {group}" + (f" (replay {len(only)} row(s))" if only else ""))
        stop_server(f"{label} {group} stop")
        wipe(table.get("dataPaths") or [f"plugins/{plugin_name}"])
        for name in deployed:
            docker_exec("rm", "-f", f"{SERVER_ROOT}/plugins/{name}")
        deployed.clear()
        for dep in DEPENDENCY_JARS:
            deploy_jar(dep)
            deployed.append(os.path.basename(dep))
        deploy_jar(jar)
        deployed.append(os.path.basename(jar))
        version = boot(f"{label}-boot", plugin_name, package_prefix)
        if overrides:
            write_config_overrides(plugin_name, overrides)
            version = boot(f"{label}-boot", plugin_name, package_prefix)
        doc, err = run_driver(files, group, label + ("-replay" if only else ""), only)
        if doc is None:
            record("behaviour-harness", False, f"{label} {group}: {err}")
        results[group] = doc
    return results, version


def compare_rows(a, b):
    """Mirror of the driver's --compare: same | changed | message-changed | not-compared."""
    def without_refusal(o):
        o = dict(o or {})
        o.pop("refusal", None)
        return json.dumps(o, sort_keys=True)
    by_id = {r["id"]: r for r in b["rows"]}
    out = []
    for ra in a["rows"]:
        rb = by_id.get(ra["id"])
        if rb is None or ra["status"] != "observed" or rb["status"] != "observed":
            out.append((ra["id"], "not-compared", ra, rb))
            continue
        fa, fb = ra["outcome"].get("refusal", []), rb["outcome"].get("refusal", [])
        if without_refusal(ra["outcome"]) != without_refusal(rb["outcome"]) or bool(fa) != bool(fb):
            result = "changed"
        elif fa != fb:
            result = "message-changed"
        else:
            result = "same"
        out.append((ra["id"], result, ra, rb))
    return out


def not_checked_share(results):
    rows = [r for doc in results.values() if doc for r in doc["rows"]]
    return (sum(1 for r in rows if r["status"] != "observed") / len(rows)) if rows else 1.0, len(rows)


def judge(table, files, plugin_name, package_prefix, baseline, candidate, deployed):
    """Harness share, comparison, replay of changed rows on both jars, verdict. Ends the run."""
    # The candidate's observed behaviour as a "who can do what" page (gates/behaviour_page.py).
    try:
        import behaviour_page
        page = behaviour_page.render(table, candidate, f"v{RESULT.get('version')}")
        with open(os.path.join(WORK_DIR, "evidence", "behaviour.md"), "w") as f:
            f.write(page)
    except Exception as exc:  # evidence only; never masks the verdict
        print(f"  behaviour page not written: {exc}")
    for label, results in (("stable", baseline), ("candidate", candidate)):
        share, n = not_checked_share(results)
        if share >= MAX_NOT_CHECKED:
            record("behaviour-harness", False, f"{label}: {share:.0%} of {n} rows not checked (limit {MAX_NOT_CHECKED:.0%}) — no verdict")
    record("behaviour-harness", True, "both passes ran; " + ", ".join(
        f"{label} {not_checked_share(r)[0]:.0%} not checked" for label, r in (("stable", baseline), ("candidate", candidate))))

    compared = {g: compare_rows(baseline[g], candidate[g]) for g in table["configGroups"] if baseline.get(g) and candidate.get(g)}
    changed = {g: [rid for rid, res, _, _ in rows if res == "changed"] for g, rows in compared.items()}

    # A changed row is replayed once on both jars; it counts only if it changes again.
    confirmed = {}
    if any(changed.values()):
        print(f"\n[replay] {sum(len(v) for v in changed.values())} changed row(s) on both jars")
        rb, _ = play(BASELINE_JAR, "stable", table, files, plugin_name, package_prefix, deployed, changed)
        rc, _ = play(CANDIDATE_JAR, "candidate", table, files, plugin_name, package_prefix, deployed, changed)
        for g, ids in changed.items():
            if not ids:
                continue
            again = {rid for rid, res, _, _ in compare_rows(rb[g], rc[g]) if res == "changed"}
            confirmed[g] = [rid for rid in ids if rid in again]

    for g, rows in compared.items():
        for rid, res, ra, rb_ in rows:
            if res == "changed" and rid not in confirmed.get(g, []):
                res = "flaky"  # changed once, not on replay
            RESULT["rows"].append({"group": g, "id": rid, "result": res,
                                   "stable": ra.get("outcome") if ra and ra["status"] == "observed" else (ra or {}).get("why"),
                                   "candidate": rb_.get("outcome") if rb_ and rb_["status"] == "observed" else (rb_ or {}).get("why")})
    real = [r for r in RESULT["rows"] if r["result"] == "changed"]
    msg = [r for r in RESULT["rows"] if r["result"] == "message-changed"]
    flaky = [r for r in RESULT["rows"] if r["result"] == "flaky"]
    detail = f"{len(real)} changed, {len(msg)} message-changed, {len(flaky)} changed once but not on replay"
    if real:
        detail += ": " + ", ".join(f"{r['group']}/{r['id']}" for r in real)
    RESULT["assertions"].append({"name": "behaviour-diff", "passed": not real, "detail": detail})
    print(f"  {'PASS' if not real else 'FAIL'}: behaviour-diff — {detail}")
    stop_server("final stop")
    finish(not real)


def shards(table, size=None):
    """The table's rows cut into pass-sized pieces, one config group at a time, in table order:
    [{"key": "<group>-<n>", "group": ..., "ids": [...]}]. `plan` and `compare` both call this, so
    the shards a run expects are the shards it planned."""
    size = size or SHARD_SIZE
    out = []
    for group in table["configGroups"]:
        ids = [r["id"] for r in table["rows"] if r["group"] == group]
        for i in range(0, len(ids), size):
            out.append({"key": f"{group}-{i // size}", "group": group, "ids": ids[i:i + size]})
    return out


def load_files(lang_from=None):
    files = {
        "rows": fetch(ROWS_URL, os.path.join(WORK_DIR, "behaviour.json")),
        "setup": fetch(SETUP_URL, os.path.join(WORK_DIR, "behaviour-setup.js")) if SETUP_URL else None,
        "driver": fetch(DRIVER_URL, os.path.join(WORK_DIR, "behaviour-driver.js")) if DRIVER_URL else None,
        "lang": extract_lang(lang_from, os.path.join(WORK_DIR, "lang_en_US.properties")) if lang_from else None,
    }
    return files, json.load(open(files["rows"]))


def plan_main():
    """MODE=plan: print the pass matrix as JSON for the workflow (one entry per shard and side)."""
    os.makedirs(WORK_DIR, exist_ok=True)
    _, table = load_files()
    matrix = [{"side": side, **sh, "ids": ",".join(sh["ids"])} for sh in shards(table) for side in ("stable", "candidate")]
    print(json.dumps({"include": matrix}))


def pass_main():
    """MODE=pass: play one shard (SHARD) on one jar (SIDE) on a fresh server, and write the outcome
    beside the boot assertions in RESULT_PATH; `compare` judges. The candidate's plugin.yml and lang
    file are used for both sides, as in single mode."""
    print(f"=== Behaviour gate (T4) pass: {SIDE} {SHARD} ===\n")
    os.makedirs(os.path.join(WORK_DIR, "evidence"), exist_ok=True)
    meta = read_plugin_yml(CANDIDATE_JAR)
    plugin_name, package_prefix = meta.get("name"), package_of(meta)
    RESULT["plugin"] = plugin_name
    files, table = load_files(lang_from=CANDIDATE_JAR)
    shard = next((sh for sh in shards(table) if sh["key"] == SHARD), None)
    if shard is None:
        record("behaviour-table", False, f"no shard {SHARD!r} in the table")
    if not wait_for(lambda: DONE_LINE.search(logs_since(datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))) is not None, 300, "first Done", poll=10):
        record(f"{'baseline' if SIDE == 'stable' else 'candidate'}-boot", False, "the server never reached Done before any jar was deployed")
    jar = BASELINE_JAR if SIDE == "stable" else CANDIDATE_JAR
    results, version = play(jar, SIDE, table, files, plugin_name, package_prefix, [], {shard["group"]: shard["ids"]})
    RESULT["pass"] = {"side": SIDE, "shard": SHARD, "group": shard["group"], "version": version, "doc": results.get(shard["group"])}
    stop_server("pass stop")
    finish(True)


def compare_main():
    """MODE=compare: gather every pass job's result, require one per planned shard and side, then
    judge exactly as single mode does (the replay of changed rows boots both jars here)."""
    print("=== Behaviour gate (T4) compare ===\n")
    os.makedirs(os.path.join(WORK_DIR, "evidence"), exist_ok=True)
    meta = read_plugin_yml(CANDIDATE_JAR)
    plugin_name, package_prefix = meta.get("name"), package_of(meta)
    RESULT["plugin"] = plugin_name
    files, table = load_files(lang_from=CANDIDATE_JAR)
    if table.get("plugin") != plugin_name:
        record("behaviour-table", False, f"table is for {table.get('plugin')!r}, candidate is {plugin_name!r}")
    record("behaviour-table", True, f"{len(table['rows'])} rows, groups {list(table['configGroups'])}, "
                                    f"{len(shards(table))} shard(s) of up to {SHARD_SIZE}")
    passes = {}
    for root, _, names in os.walk(PASS_DIR):
        for name in names:
            if name == "pass.json":
                doc = json.load(open(os.path.join(root, name)))
                info = doc.get("pass") or {}
                passes[(info.get("side"), info.get("shard"))] = doc
    sides = {"stable": {}, "candidate": {}}
    for sh in shards(table):
        for side in sides:
            doc = passes.get((side, sh["key"]))
            boot_name = "baseline-boot" if side == "stable" else "candidate-boot"
            if doc is None:
                record("behaviour-harness", False, f"no result from the {side} pass of shard {sh['key']} — no verdict")
            if not doc.get("passed"):
                failed = next((a for a in doc.get("assertions", []) if not a.get("passed")), {})
                name = failed.get("name") or "behaviour-harness"
                record(name if name in (boot_name, "behaviour-harness") else "behaviour-harness", False,
                       f"{side} pass {sh['key']}: {failed.get('name')}: {failed.get('detail', '')}")
            info = doc["pass"]
            if side == "stable":
                RESULT["baselineVersion"] = info.get("version")
            else:
                RESULT["version"] = info.get("version")
            merged = sides[side].setdefault(sh["group"], {"rows": []})
            merged["rows"] += (info.get("doc") or {}).get("rows", [])
            # Keep each pass's driver output as evidence.
            with open(os.path.join(WORK_DIR, "evidence", f"{side}-{sh['key']}.json"), "w") as f:
                json.dump(info.get("doc"), f, indent=1)
    record("baseline-boot", True, f"v{RESULT['baselineVersion']}")
    record("candidate-boot", True, f"v{RESULT['version']}")
    if not wait_for(lambda: DONE_LINE.search(logs_since(datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))) is not None, 300, "first Done", poll=10):
        record("behaviour-harness", False, "the replay server never reached Done")
    judge(table, files, plugin_name, package_prefix, sides["stable"], sides["candidate"], [])


def main():
    if MODE == "plan":
        return plan_main()
    if MODE == "pass":
        return pass_main()
    if MODE == "compare":
        return compare_main()
    print("=== Behaviour gate (T4) ===\n")
    os.makedirs(os.path.join(WORK_DIR, "evidence"), exist_ok=True)
    meta = read_plugin_yml(CANDIDATE_JAR)
    plugin_name = meta.get("name")
    package_prefix = package_of(meta)
    RESULT["plugin"] = plugin_name

    files = {
        "rows": fetch(ROWS_URL, os.path.join(WORK_DIR, "behaviour.json")),
        "setup": fetch(SETUP_URL, os.path.join(WORK_DIR, "behaviour-setup.js")),
        "driver": fetch(DRIVER_URL, os.path.join(WORK_DIR, "behaviour-driver.js")),
        "lang": extract_lang(CANDIDATE_JAR, os.path.join(WORK_DIR, "lang_en_US.properties")),
    }
    table = json.load(open(files["rows"]))
    if table.get("plugin") != plugin_name:
        record("behaviour-table", False, f"table is for {table.get('plugin')!r}, candidate is {plugin_name!r}")
    record("behaviour-table", True, f"{len(table['rows'])} rows, groups {list(table['configGroups'])}")

    if not wait_for(lambda: DONE_LINE.search(logs_since(datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))) is not None, 300, "first Done", poll=10):
        record("baseline-boot", False, "the server never reached Done before any jar was deployed")

    deployed = []
    baseline, RESULT["baselineVersion"] = play(BASELINE_JAR, "stable", table, files, plugin_name, package_prefix, deployed)
    record("baseline-boot", True, f"v{RESULT['baselineVersion']}")
    candidate, RESULT["version"] = play(CANDIDATE_JAR, "candidate", table, files, plugin_name, package_prefix, deployed)
    record("candidate-boot", True, f"v{RESULT['version']}")

    judge(table, files, plugin_name, package_prefix, baseline, candidate, deployed)



if __name__ == "__main__":
    main()
