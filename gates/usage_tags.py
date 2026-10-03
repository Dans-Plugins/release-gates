"""Mark every usage event a gate server sends as CI.

A gate server is disposable, and its startups are not installations. Two things report
usage from it, and both are told the run is CI:

  - OMCSI's minecraft-wrapper, through USAGE_REPORTING_TAGS=ci=true in the .env each
    workflow writes (compose.yml passes it through to the wrapper);
  - every plugin carrying the vendored trace client, through the server-wide switch file
    `plugins/trace/config.yml`, written here.

The wrapper's first setup empties the server root (OVERWRITE_EXISTING_SERVER=true), and a
supplied save-compatibility fixture is unpacked over it, so the file is not written once:
every harness calls `write_ci_trace_config` immediately before each /api/server/start,
which is the only way a plugin on a gate server is ever enabled.

Reporting stays on (`enabled: true`) so a CI run is counted as CI rather than not counted
at all -- unless a jar on the server carries a trace client older than 0.3.0. Such a client
reads `enabled:` from the file but not `tags:`, so its events would arrive untagged and be
counted as real installations (the save-compatibility gate boots the previous release on
purpose, and 2026-10-03 found ~260 such events in trace). For that boot the file says
`enabled: false` instead, which every client from 0.2.0 on obeys, and the server sends
nothing. A 0.1.x client reads neither; it is named in the log, since nothing here can
silence it.

Nothing here is an assertion: on success no entry is added to result.json.
"""

import io
import subprocess
import zipfile

SERVER_ROOT = "/mcserver"
TRACE_CONFIG_PATH = "plugins/trace/config.yml"  # server-root-relative
TRACE_CONFIG = 'enabled: true\ntags:\n  ci: "true"\n'
TRACE_CONFIG_DISABLED = (
    "# A plugin on this gate server carries a trace client older than 0.3.0, which\n"
    "# ignores tags: -- reporting is off so its CI boots are not counted as installations.\n"
    "enabled: false\n"
)

# What each trace-client generation leaves in TraceClient.class's constant pool.
# 0.3.0 added the `tags:` block (its line pattern); 0.2.0 added the server-wide switch
# (the reason string it reports when the file turns it off).
_TAGS_MARKER = b"^tags\\s*:"
_SWITCH_MARKER = b"server-wide config: plugins/trace/config.yml"


def classify_trace_client(jar_bytes):
    """What the trace client vendored in a jar honours.

    Returns "tags" (0.3.0+: `tags:` and `enabled:`), "switch" (0.2.x: `enabled:` only),
    "none" (0.1.x: neither), or None when the jar carries no trace client (or is not
    a readable zip). A jar with several clients is judged by its weakest."""
    try:
        with zipfile.ZipFile(io.BytesIO(jar_bytes)) as z:
            kinds = []
            for name in z.namelist():
                if name.endswith("/trace/TraceClient.class"):
                    b = z.read(name)
                    kinds.append("tags" if _TAGS_MARKER in b
                                 else "switch" if _SWITCH_MARKER in b else "none")
    except (zipfile.BadZipFile, OSError):
        return None
    for weakest in ("none", "switch", "tags"):
        if weakest in kinds:
            return weakest
    return None


def _server_jars(container):
    """{basename: classification} for every jar directly in the server's plugins folder."""
    r = subprocess.run(["docker", "exec", container, "sh", "-c",
                        f"ls -1 '{SERVER_ROOT}/plugins' 2>/dev/null"],
                       capture_output=True, timeout=30)
    names = [n for n in r.stdout.decode("utf-8", "replace").splitlines() if n.endswith(".jar")]
    found = {}
    for name in names:
        jar = subprocess.run(["docker", "exec", container, "cat", f"{SERVER_ROOT}/plugins/{name}"],
                             capture_output=True, timeout=60)
        if jar.returncode == 0:
            found[name] = classify_trace_client(jar.stdout)
    return found


def write_ci_trace_config(container):
    """Write the CI trace config into the container and read it back.

    Returns (ok, detail). Written through `docker exec` so the file is owned by the
    server's own user, like everything else under the server root."""
    path = f"{SERVER_ROOT}/{TRACE_CONFIG_PATH}"
    try:
        jars = _server_jars(container)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"plugins in {SERVER_ROOT}/plugins could not be read: {e}"
    untaggable = sorted(n for n, k in jars.items() if k == "switch")
    unsilenceable = sorted(n for n, k in jars.items() if k == "none")
    config = TRACE_CONFIG_DISABLED if untaggable else TRACE_CONFIG

    script = f"mkdir -p '{SERVER_ROOT}/plugins/trace' && cat > '{path}' && cat '{path}'"
    try:
        r = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", script],
                           input=config.encode("utf-8"), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{path} could not be written: {e}"
    written = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0 or written != config:
        return False, (f"{path} could not be written (exit {r.returncode}): "
                       f"{r.stderr.decode('utf-8', 'replace').strip() or repr(written)}")
    print(f"  {path} (CI usage tags):")
    for line in written.splitlines():
        print(f"    {line}")
    if untaggable:
        print(f"    (reporting off: trace client older than 0.3.0 in {', '.join(untaggable)})")
    if unsilenceable:
        print(f"    WARNING: trace client older than 0.2.0 in {', '.join(unsilenceable)} -- "
              "it ignores this file, so its events arrive untagged")
    return True, path
