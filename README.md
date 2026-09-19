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
| [`save-compat.yml`](.github/workflows/save-compat.yml) | The candidate loads the data the current stable release wrote — recorded live by booting that release, optionally with config overrides and console commands — migrates it, and keeps every file and every `<n> <label> loaded` count across its own restart | available |
| [`dependents.yml`](.github/workflows/dependents.yml) | Every plugin that `depend:`s on the candidate — each one's current stable release — still enables when the candidate replaces the dependency it was built against, across a clean stop and a second boot | available |

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
| `dpm_jar_url` | no | a Dan's Plugin Manager jar to deploy instead of its `/releases/latest` one — how a candidate build of the installer itself is verified; `result.json` records `dpmSource: "input"` or `"latest"` |

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

## Save-compatibility gate

Proves that a candidate loads what the current stable release saved. The stable jar
(`baseline_jar_url`) is booted on a fresh server, optionally given config overrides and a
sequence of console commands so it has something to save, and restarted over its own data.
Everything under `plugins/<Name>/` (plus any `extra_data_paths`) is then captured as a fixture
and the candidate is booted over it twice — once to migrate, once to prove it can read back
what it wrote itself.

Inputs, identical for `workflow_dispatch` and `workflow_call`:

| Input | Required | Meaning |
|---|---|---|
| `repository` | yes | `owner/repo` of the plugin — recorded in the result |
| `sha` | yes | commit the candidate was built from — recorded in the result |
| `jar_url` | yes | where to download the candidate |
| `baseline_jar_url` | yes | where to download the current stable jar whose data the candidate must load |
| `dependencies` | no | comma-separated `owner/repo` list; each one's `/releases/latest` jar is installed beside both jars, exactly as in the boot gate |
| `expected_version` | no | the version the candidate must report when enabling |
| `minecraft_version` | no | Spigot version to boot (default `26.2`) |
| `config_overrides` | no | newline-separated `dotted.key: value` lines applied to `plugins/<Name>/config.yml` after the baseline's first boot (the file is re-serialised, so comments are lost); `<Name>` is the plugin.yml `name:` |
| `scenario` | no | newline-separated console commands sent to the baseline, about 3 s apart, after the overrides and a restart |
| `extra_data_paths` | no | newline-separated server-root-relative glob patterns to capture beyond `plugins/<Name>/`, e.g. `medieval_factions_db*` for a plugin whose H2 file sits in the server root |

Assertions, in order — the run stops at the first failure:

1. **baseline-boot** — dependency jars and the baseline jar are deployed; after a restart the
   baseline logs `Enabling <name> v<version>` with no enable failure and no error attributable
   to it.
2. **config-overrides** — the overrides are applied and the baseline enables again over the
   edited config. Passes with "none" when no overrides were given.
3. **scenario** — each command is sent over the console; once the console has been quiet for
   5 s, no error is attributable to the baseline. Passes with "none" when no scenario was given.
4. **baseline-restart** — the baseline enables over its own data. Every `<n> <label> loaded`
   line it prints during this boot (Medieval Factions prints `3 factions loaded (5ms)`) is
   captured as the reference count for that label.
5. **fixture** — the server is stopped; `plugins/<Name>/` and every path matched by
   `extra_data_paths` are copied out, listed (size and sha256) and archived. At least one file
   must have been captured.
6. **candidate-boot-1** — the baseline jar is removed and the candidate deployed; the candidate
   logs `Enabling <name> v<version>` (equal to `expected_version` when given); no enable
   failure, no `Could not load` / `UnknownDependencyException`, no `ERROR`/`SEVERE` line naming
   the plugin and no stack frame inside its package.
7. **counts-1** — every label the baseline logged is logged by the candidate with the same
   count. A label that appears on only one side is reported, not failed (plugins change their
   log lines). Passes with "baseline logged no counts" when there were none.
8. **files-kept-1** — every file in the fixture still exists: at its path (content may change),
   or — a migration — as a byte-identical copy under the same name inside `plugins/<Name>/`.
   A removed file fails; a same-named file with different content does not count as migrated.
   Added files are reported.
9. **stop-1** — the server stops within two minutes with no error attributable to the candidate, **and the database closed cleanly**: no `*.trace.db` appeared anywhere under the server root and the console has no `zip file closed` / `MVStoreException` / `OnExitDatabaseCloser` line. An embedded store that fails to close on shutdown is a slow, silent path to a corrupt save, so it blocks regardless of how the boot looked.
10. **candidate-boot-2**, **counts-2**, **files-kept-2**, **stop-2** — the same, over the data
    the candidate itself wrote.

What it does not prove: anything about data the scenario did not create (an empty scenario
proves only that the baseline's freshly written defaults load), that a downgrade back to the
baseline works, or that migrated values are semantically right — only that the files and the
counts the plugin reports survive.

Dispatch by hand:

```
gh workflow run save-compat.yml --repo Dans-Plugins/release-gates \
  -f repository=Dans-Plugins/Medieval-Factions \
  -f sha=<commit> \
  -f jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/dev/<jar> \
  -f baseline_jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/v5.8.1/<jar> \
  -f config_overrides='factions.allowLeaderlessFactions: true' \
  -f scenario=$'faction admin create Alpha\nfaction admin create Bravo' \
  -f extra_data_paths='medieval_factions_db*'
```

## Dependents gate

Proves that the plugins built against the candidate's predecessor still boot on the
candidate. The candidate, any `dependencies` jars and the current stable release of every
`dependents` repository are deployed together on one fresh server, which is booted, stopped
cleanly and booted again. It is the gate for a plugin other plugins `depend:` on — Medieval
Factions, whose API Currencies, Fiefs, Democracy and the BlueMap integration compile
against — and it answers the question the boot gate cannot: does the candidate still carry
what its dependents link to.

Inputs, identical for `workflow_dispatch` and `workflow_call`:

| Input | Required | Meaning |
|---|---|---|
| `repository` | yes | `owner/repo` of the plugin — recorded in the result |
| `sha` | yes | commit the candidate was built from — recorded in the result |
| `jar_url` | yes | where to download the candidate |
| `dependents` | yes | comma-separated `owner/repo` list of plugins that depend on the candidate; each one's `/releases/latest` jar is the dependent under test. A repository with no stable release is reported as `skipped: no stable release` and does not fail the gate — there is nothing an operator could be running. A repository that does not exist fails the run |
| `dependencies` | no | comma-separated `owner/repo[#asset-substring]` list of *other* plugins the dependents need besides the candidate; each one's `/releases/latest` jar is installed alongside. `#substring` narrows a release that ships one jar per platform (`BlueMap-Minecraft/BlueMap#spigot`). Usually empty |
| `expected_version` | no | the version the candidate must report when enabling |
| `minecraft_version` | no | Spigot version to boot (default `26.2`) |

Assertions, in order:

1. **install-<Name>** — the dependent's stable jar carries a readable `plugin.yml`, and
   every plugin it hard-depends on is on the server: the candidate, a `dependencies` jar or
   another dependent. A dependent whose other dependency was not supplied is reported as
   `skipped: dependency [...] not supplied` and not deployed — the candidate did not break
   it, the caller did not supply it.
2. **boot-1** — the server reaches `Done` (fatal).
3. **candidate-enabled-1** — the candidate logs `Enabling <name> v<version>` (equal to
   `expected_version` when given); no enable failure, no `Could not load`, no
   `ERROR`/`SEVERE` line naming it and no stack frame inside its package. Fatal: with the
   candidate down nothing about its dependents can be concluded.
4. **enable-<Name>-1** — for every installed dependent: it logs `Enabling <Name> v…`; no
   `Error occurred while enabling <Name>`; no `Could not load 'plugins/<jar>'` /
   `UnknownDependencyException`; no `ERROR`/`SEVERE` line naming it and no stack frame
   inside its package. The exception line Bukkit prints after an enable failure
   (`NoClassDefFoundError`, `NoSuchMethodError`…) is quoted in the detail — it names the
   symbol the candidate no longer carries.
5. **stop-1** — the server stops within two minutes with no error attributable to the
   candidate or any dependent, **and the database closed cleanly**: no `*.trace.db`
   appeared under the server root and the console has no `zip file closed` /
   `MVStoreException` / `OnExitDatabaseCloser` line, the same check as the
   save-compatibility gate.
6. **boot-2**, **candidate-enabled-2**, **enable-<Name>-2**, **stop-2** — the same, over
   the data folders the first boot wrote.

The per-dependent assertions run to completion: one dependent that breaks does not hide
whether the others still boot. The gate passes only when every assertion holds — every
dependent with a stable release enabled on both boots.

What it does not prove: that a dependent *works* against the candidate beyond enabling
(a method it calls only from a command can still be gone), or anything about dependents
that have no stable release.

Dispatch by hand:

```
gh workflow run dependents.yml --repo Dans-Plugins/release-gates \
  -f repository=Dans-Plugins/Medieval-Factions \
  -f sha=<commit> \
  -f jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/dev/<jar> \
  -f dependents=Dans-Plugins/Currencies,Dans-Plugins/Fiefs,Dans-Plugins/Democracy,Dans-Plugins/Bluemap_MedievalFactions \
  -f dependencies=BlueMap-Minecraft/BlueMap#spigot
```

## Evidence

Every run uploads an artifact `<gate>-<run id>` containing `result.json` and `server.log`
(the full console). The job summary shows the assertion table.

- Boot gate: `boot-gate-<run id>` —
  `{gate, repository, sha, plugin, version, candidateSha256, passed, assertions: [{name, passed, detail}]}` — `candidateSha256` is the digest of the exact jar that was verified, for the publisher to check before uploading
  plus the plugin's data folder.
- Install gate: `dpm-install-<run id>` —
  `{gate, dpm, dpmSource, dpmVersion, plugins: [{slug, name, version, tag, installed, enabled}], passed, assertions}`.
- Save-compatibility gate: `save-compat-<run id>` —
  `{gate, repository, sha, plugin, baselineVersion, version, passed, counts: {label: [baseline, candidate 1, candidate 2]}, fixtureFiles, assertions}`
  plus `fixture.tar.gz` (what the baseline wrote), `fixture-listing.json` and
  `candidate-data.tar.gz` (the same paths after the candidate's last boot).
- Dependents gate: `dependents-<run id>` —
  `{gate, repository, sha, plugin, version, candidateSha256, dependents: [{repository, name, version, tag, jar, installed, enabled_1, enabled_2, skipped}], passed, assertions}`.

## Design notes

- The server is disposable: offline mode, a placeholder operator UUID, no Discord, no agent.
- The OMCSI checkout and Docker image cache are the pattern from
  [Dan's Plugin Manager's integration tests](https://github.com/Dans-Plugins/Dans-Plugin-Manager/blob/main/.github/workflows/integration.yml);
  on a cache hit the Spigot build is skipped and a gate costs about ten minutes.
- Dependencies come from `/releases/latest`, the endpoint DPM's stable channel reads, and the
  Maven shade plugin's `original-*.jar` is skipped when a release carries both.
- The harness reads the console with `docker logs --since`, scoped per boot, so an error from
  the baseline boot cannot be blamed on the candidate.
