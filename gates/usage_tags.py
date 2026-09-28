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
which is the only way a plugin on a gate server is ever enabled. Reporting stays on
(`enabled: true`) so a CI run is counted as CI rather than not counted at all.

Nothing here is an assertion: on success no entry is added to result.json.
"""

import subprocess

SERVER_ROOT = "/mcserver"
TRACE_CONFIG_PATH = "plugins/trace/config.yml"  # server-root-relative
TRACE_CONFIG = 'enabled: true\ntags:\n  ci: "true"\n'


def write_ci_trace_config(container):
    """Write the CI trace config into the container and read it back.

    Returns (ok, detail). Written through `docker exec` so the file is owned by the
    server's own user, like everything else under the server root."""
    path = f"{SERVER_ROOT}/{TRACE_CONFIG_PATH}"
    script = f"mkdir -p '{SERVER_ROOT}/plugins/trace' && cat > '{path}' && cat '{path}'"
    try:
        r = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", script],
                           input=TRACE_CONFIG.encode("utf-8"), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{path} could not be written: {e}"
    written = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0 or written != TRACE_CONFIG:
        return False, (f"{path} could not be written (exit {r.returncode}): "
                       f"{r.stderr.decode('utf-8', 'replace').strip() or repr(written)}")
    print(f"  {path} (CI usage tags):")
    for line in written.splitlines():
        print(f"    {line}")
    return True, path
