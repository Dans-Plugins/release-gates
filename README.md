# release-gates

Reusable GitHub Actions workflows that prove a Dans-Plugins plugin build is fit to be a stable
release. Each gate boots the candidate on a real Spigot server
([OMCSI](https://github.com/dmccoystephenson/open-mc-server-infrastructure) in Docker) and
fails on the first assertion that does not hold. The run log and a `result.json` artifact are
the evidence; the policy that decides what to do with that evidence lives elsewhere (see
[Release channels](https://github.com/Dans-Plugins/dpc-conventions/blob/main/docs/RELEASE_CHANNELS.md)).

These workflows publish nothing and hold no secrets. Their token is read-only.

## Gates

| Workflow | Proves | Status |
|---|---|---|
| [`boot-gate.yml`](.github/workflows/boot-gate.yml) | The candidate enables on a fresh server with its dependencies' current stable releases, answers `help` for every command it declares, stops cleanly, and enables again over the data folder it wrote | available |
| [`dpm-install.yml`](.github/workflows/dpm-install.yml) | `/dpm get <slug>` on a fresh server installs each plugin's current stable release through [Dan's Plugin Manager](https://github.com/Dans-Plugins/Dans-Plugin-Manager), and the installed plugins enable — the acceptance test for a stable release | available |
| `save-compat.yml` | The candidate loads a fixture recorded by the previous stable release ([plugin-fixtures](https://github.com/Dans-Plugins/plugin-fixtures)) without loss | planned |
| `dependents.yml` | Every plugin that depends on the candidate still boots against it | planned |

## Boot gate

Inputs, identical for `workflow_dispatch` and `workflow_call`:

| Input | Required | Meaning |
|---|---|---|
| `repository` | yes | `owner/repo` of the plugin — recorded in the result |
| `sha` | yes | commit the candidate was built from — recorded in the result |
| `jar_url` | yes | where to download the candidate |
| `dependencies` | no | comma-separated `owner/repo` list; each one's `/releases/latest` jar is installed beside the candidate, exactly what an operator would get |
| `expected_version` | no | the version the candidate must report when enabling |
| `minecraft_version` | no | Spigot version to boot (default `26.2`) |

Assertions, in order — the run stops at the first failure:

1. **dependencies** — every `depend:` in the candidate's `plugin.yml` is satisfied by a supplied jar.
2. **boot-1** — the server reaches `Done`; the candidate logs `Enabling <name> v<version>`; no enable failure; no `ERROR`/`SEVERE` line naming the plugin and no stack frame inside its package.
3. **version** — the enabled version equals `expected_version` (when given).
4. **plugins-1** — `plugins` lists the candidate.
5. **commands-1** — for every command under `commands:` in the candidate's `plugin.yml`, `help <command>` is answered with a help topic within 20 seconds and not with `No help for` / `Unknown command`. A plugin with no commands passes with "no commands declared".
6. **stop-1** — the server stops within two minutes with no error attributable to the plugin.
7. **boot-2**, **plugins-2**, **commands-2**, **stop-2** — the same, booted over the data folder the first boot created. A plugin that writes a file it cannot read back fails here.

Dispatch by hand:

```
gh workflow run boot-gate.yml --repo Dans-Plugins/release-gates \
  -f repository=Dans-Plugins/Easy-Links \
  -f sha=<commit> \
  -f jar_url=https://github.com/Dans-Plugins/Easy-Links/releases/download/dev/<jar> \
  -f expected_version=0.4.0
```

Reuse from a plugin repository — reference a tag, never a branch, so a change here cannot alter a check in flight:

```yaml
jobs:
  boot:
    uses: Dans-Plugins/release-gates/.github/workflows/boot-gate.yml@v1
    with:
      repository: ${{ github.repository }}
      sha: ${{ github.sha }}
      jar_url: https://github.com/${{ github.repository }}/releases/download/dev/MyPlugin.jar
```

## Install gate

Proves what an operator gets: the current stable release of Dan's Plugin Manager is deployed
on a fresh server, `dpm get <slug>` is sent over the console for each slug, and the server is
restarted so the downloaded jars enable. Nothing is downloaded by the harness itself — DPM
resolves each slug to its repository's `/releases/latest` exactly as it does on a live server.

Inputs, identical for `workflow_dispatch` and `workflow_call`:

| Input | Required | Meaning |
|---|---|---|
| `plugins` | yes | comma-separated DPM slugs, as listed by `/dpm list` (e.g. `easylinks,herald`) |
| `minecraft_version` | no | Spigot version to boot (default `26.2`) |

Assertions, in order:

1. **dpm** — the server reaches `Done` with Dan's Plugin Manager enabled (fatal).
2. **get-<slug>** — `dpm get <slug>` reports `Downloaded` (or `already up to date`) rather than
   `Plugin not found`, a GitHub error, or no release; and the jar DPM wrote to `plugins/<slug>.jar`
   carries a readable `plugin.yml`, from which the plugin's real name is taken.
3. **boot** — the server reaches `Done` over the plugins DPM installed (fatal).
4. **enable-<slug>** — the plugin logs `Enabling <name> v<version>`; no enable failure; no
   `ERROR`/`SEVERE` line naming it and no stack frame inside its package.
5. **stop** — the server stops within two minutes with no error attributable to any of them.

Unlike the boot gate, the per-plugin assertions run to completion: one slug DPM cannot find
does not hide whether the others install. The gate passes only when every assertion holds.

Dispatch by hand:

```
gh workflow run dpm-install.yml --repo Dans-Plugins/release-gates \
  -f plugins=easylinks,foodspoilage,herald
```

## Evidence

Every run uploads an artifact `<gate>-<run id>` containing `result.json` and `server.log`
(the full console). The job summary shows the assertion table.

- Boot gate: `boot-gate-<run id>` —
  `{gate, repository, sha, plugin, version, passed, assertions: [{name, passed, detail}]}`
  plus the plugin's data folder.
- Install gate: `dpm-install-<run id>` —
  `{gate, dpm, dpmVersion, plugins: [{slug, name, version, tag, installed, enabled}], passed, assertions}`.

## Design notes

- The server is disposable: offline mode, a placeholder operator UUID, no Discord, no agent.
- The OMCSI checkout and Docker image cache are the pattern from
  [Dan's Plugin Manager's integration tests](https://github.com/Dans-Plugins/Dans-Plugin-Manager/blob/main/.github/workflows/integration.yml);
  on a cache hit the Spigot build is skipped and a gate costs about ten minutes.
- Dependencies come from `/releases/latest`, the endpoint DPM's stable channel reads, and the
  Maven shade plugin's `original-*.jar` is skipped when a release carries both.
- The harness reads the console with `docker logs --since`, scoped per boot, so an error from
  the baseline boot cannot be blamed on the candidate.
