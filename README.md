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
| [`save-compat.yml`](.github/workflows/save-compat.yml) | The candidate loads the data the current stable release wrote — recorded live by booting that release, optionally with config overrides, console commands and a bot scenario that plays it as players, or a published fixture such as an anonymised real-world database — migrates it, and keeps every file and every `<n> <label> loaded` count across any number of its own restarts; on the embedded H2 store, an external MariaDB or PostgreSQL, or the plugin's JSON store with a migration round trip | available |
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
and the candidate is booted over it — once to migrate, then `restart_cycles` more times to
prove it can read back what it wrote itself, and keeps doing so. The whole exercise runs on
one of four stores (`backend`): the plugin's embedded H2, an external MariaDB or PostgreSQL
in a sibling container, or the plugin's JSON files — see [Backend matrix](#backend-matrix).

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
| `fixture_url` | no | URL of a fixture `tar.gz` — a [plugin-fixtures](https://github.com/Dans-Plugins/plugin-fixtures) release asset — to boot the baseline over instead of recording one; see [Supplied fixtures](#supplied-fixtures) |
| `fixture_manifest_url` | no | URL of that fixture's `manifest.json`; defaults to `manifest.json` beside the archive, which every plugin-fixtures release carries |
| `restart_cycles` | no | how many full stop/start cycles the candidate performs after its first boot over the fixture (default `1`); every boot carries the counts, files-kept and stop checks. The release automation passes `5` for a database-backed plugin: a store that fails to close shows it on a later boot, not the first |
| `backend` | no | the store the plugin is run on: `h2` (default — the plugin's embedded default, today's behaviour), `mariadb`, `postgres` (a database container beside the server, shared by baseline and candidate) or `json` (the plugin's JSON files, plus a migration round trip). See [Backend matrix](#backend-matrix) |
| `scenario_script` | no | raw URL of a Node/mineflayer script (normally `scenarios/<slug>.js` in [plugin-fixtures](https://github.com/Dans-Plugins/plugin-fixtures)) run against the baseline after the console scenario — see [Bot scenarios](#bot-scenarios) |

Assertions, in order — the run stops at the first failure:

0. **backend** — the backend is one of the four; for `mariadb`/`postgres` the database
   container accepts connections (a failure here is the runner's, not the plugin's).
1. **fixture-unpack** — only with `fixture_url`: the server is stopped after its first start
   and the archive is unpacked into the server root as the server's own user (a single
   wrapper directory, as in the gate's own `fixture.tar.gz` evidence, is stripped); every
   path the manifest lists must then exist. On a database backend a `db-dump.sql` in the
   archive — what this gate's own `fixture` step records — is restored into the database.
2. **baseline-boot** — dependency jars and the baseline jar are deployed; after a restart the
   baseline logs `Enabling <name> v<version>` with no enable failure and no error attributable
   to it. With a supplied fixture this boot is already over that data. When Bukkit prints an
   exception after `Error occurred while enabling`, that line is quoted in the detail.
3. **config-overrides** — the overrides are applied — the backend's own keys on top of the
   caller's — and the baseline enables again over the edited config. Passes with "none" when
   there are none.
4. **scenario** — each command is sent over the console; once the console has been quiet for
   5 s, no error is attributable to the baseline. Passes with "none" when no scenario was given.
   Then **bot-scenario** — the `scenario_script` is fetched and run against the baseline with
   bots joined to the server; it must exit 0 within 15 minutes, and no error may be
   attributable to the baseline while it runs. Its stdout is the evidence (`bot-scenario.log`).
   Passes with "none" when no script was given.
5. **baseline-restart** — the baseline enables over its own data. Every `<n> <label> loaded`
   line it prints during this boot (Medieval Factions prints `3 factions loaded (5ms)`) is
   captured as the reference count for that label. Then **bot-scenario-counts** — the counts
   the script said to expect (its `SCENARIO_EXPECTED` line) match what the scenario *added*:
   those reference counts minus what the baseline's first boot loaded (zero on a fresh
   server, the fixture's contents when `fixture_url` was given), label by label; a label the
   baseline does not log is reported. Passes with "none" when no script was given.
6. **fixture-expected** — only with `fixture_url`, and judged on the baseline's *first* boot
   over the fixture (step 3, before any scenario adds to it): every label in the manifest's
   `expected` is logged by the baseline with that count. A stable release that does not load what the
   fixture promises is a finding about the stable release, and nothing about the candidate
   can be concluded from it.
7. **fixture** — the server is stopped; `plugins/<Name>/` and every path matched by
   `extra_data_paths` are copied out, listed (size and sha256) and archived. At least one file
   must have been captured. With a supplied fixture this is that fixture as the baseline left it.
   On a database backend the database's contents are dumped beside them as `db-dump.sql`.
8. **candidate-boot-1** — the baseline jar is removed and the candidate deployed; the candidate
   logs `Enabling <name> v<version>` (equal to `expected_version` when given); no enable
   failure, no `Could not load` / `UnknownDependencyException`, no `ERROR`/`SEVERE` line naming
   the plugin and no stack frame inside its package.
9. **counts-1** — every label the baseline logged is logged by the candidate with the same
   count. A label that appears on only one side is reported, not failed (plugins change their
   log lines). Passes with "baseline logged no counts" when there were none. With a supplied
   fixture, every label in the manifest's `expected` must also be logged with that count plus
   whatever the scenario added on top of the fixture (step 5's delta) — a label the manifest
   promises and the candidate no longer logs fails here.
10. **files-kept-1** — every file in the fixture still exists: at its path (content may change),
   or — a migration — as a byte-identical copy under the same name inside `plugins/<Name>/`.
   A removed file fails; a same-named file with different content does not count as migrated.
   Added files are reported. On a database backend `db-dump.sql` is re-dumped and tracked like
   any other file: it must still be producible, and its content is expected to change.
11. **stop-1** — the server stops within two minutes with no error attributable to the candidate, **and the database closed cleanly**: no `*.trace.db` under the server root that is new or has grown since the fixture was recorded, and the console has no `zip file closed` / `MVStoreException` / `OnExitDatabaseCloser` line. A trace file the *baseline* left behind on its own shutdowns is reported in `result.json` as `baselineCloseFailure` — a finding about the current stable release — and is not held against the candidate. An embedded store that fails to close on shutdown is a slow, silent path to a corrupt save, so it blocks regardless of how the boot looked. An external database leaves no trace file; the console lines are still checked.
12. **candidate-boot-k**, **counts-k**, **files-kept-k**, **stop-k** for k = 2 … `restart_cycles` + 1
    — the same, over the data the candidate itself wrote, one full stop/start cycle per k.
13. **migration-roundtrip** (`backend: json` only) — the candidate is booted on the store it
    has been running on, its storage migration command is run to the other store, the
    server is restarted on that store and every count must equal the baseline's (and the
    manifest's `expected`, with a supplied fixture); the original store is then emptied (its
    files are kept as evidence), the command is run back, and the restart on the original
    store must reproduce the counts again. Every stop on the way carries the full stop
    check. Passes as "not applicable" when the candidate does not answer the migration
    command with its usage.

What it does not prove: anything about data the scenario did not create (an empty scenario
proves only that the baseline's freshly written defaults load), that a downgrade back to the
baseline works, or that migrated values are semantically right — only that the files and the
counts the plugin reports survive.

### Bot scenarios

Console commands only reach what a plugin lets a non-player sender do — for Medieval
Factions that is `faction admin create` and nothing that a player does: claiming, allying,
locking a chest, building a gate. A `scenario_script` closes that gap. It is a single Node
file (the convention is `scenarios/<slug>.js` in
[plugin-fixtures](https://github.com/Dans-Plugins/plugin-fixtures)) that the workflow fetches
and runs, after the console scenario and before the baseline's restart, as

```
node <script> --host localhost --port 25565 --rcon-port 25575 \
     --rcon-password <the run's RCON_PASSWORD> --bots 2 \
     --server-log docker:open-mc-server --json-out <evidence>/bot-scenario.json
```

with `mineflayer` 4.39.0 and `minecraft-data` 3.116.0 installed beside it on Node 22 (the
pins are in the workflow; mineflayer 4.38+ needs Node 22). The server runs in offline mode,
so bots join under any name and are not operators. The script's contract, enforced by
`gates/scenario_runner.py`:

- exit 0 only when every step verified — a step is verified by reading state back (chat
  replies, `/f info`-style commands, RCON `execute if block`), never by trusting the client;
- print evidence to stdout: it is copied into the run log and to `bot-scenario.log`;
- optionally end stdout with `SCENARIO_EXPECTED {"<label>": <n>, ...}`, keyed by the labels
  the plugin prints in its `<n> <label> loaded` startup lines. The baseline's restart counts
  are then asserted against it (**bot-scenario-counts**), so a step the script believed
  succeeded but the plugin never persisted is caught at the next boot.

A server version the installed mineflayer does not know cannot be joined: `minecraft-data`
3.116.0 knows up to `26.1` (protocol 775) and Spigot `26.2` speaks 776, so a bot scenario
has to be dispatched with `minecraft_version=26.1` until the PrismarineJS stack publishes
26.2 support (a 26.2 run fails `bot-scenario` in one line saying exactly that). The fixture
is then recorded on 26.1, which its manifest records; it proves the plugin's save format,
not its behaviour on 26.2.

### Backend matrix

The config keys the backends apply are Medieval Factions' — the flagship, whose save
integrity is what this gate exists for. Another plugin is served by `h2` (no overrides) plus
its own `config_overrides`. Backend keys are applied *after* the caller's, so a caller cannot
point a `mariadb` run back at H2 by accident.

| `backend` | What runs | Overrides applied to the baseline's config | Fixture | Notes |
|---|---|---|---|---|
| `h2` | the plugin's embedded H2 file, exactly as an operator gets it by default | none | `plugins/<Name>/` + `extra_data_paths` | the `*.trace.db` check is what catches a store that fails to close |
| `mariadb` | `mariadb:11` in a container named `mfdb` on the server's compose network, started before any jar is deployed and kept for the whole run | `database.url: jdbc:mariadb://mfdb:3306/mf`, `database.dialect: MARIADB`, `database.username`/`password` | the same, plus `db-dump.sql` (`mariadb-dump --skip-dump-date`) | one database spans both phases: the candidate migrates the schema the stable created (Flyway), which is the point |
| `postgres` | `postgres:16`, likewise | `database.url: jdbc:postgresql://mfdb:5432/mf`, `database.dialect: POSTGRES`, username/password | the same, plus `db-dump.sql` (`pg_dump`) | the dialect names are jOOQ's `SQLDialect` enum names, which is how the plugin reads the key |
| `json` | the plugin's JSON store | `storage.type: json`, `storage.json.path: ./medieval_factions_data` — **only when the baseline's config declares `storage.type`**; a baseline without a JSON store (Medieval Factions 5.x) stays on its default store and the fixture is that store's | `plugins/<Name>/` + the JSON directory (+ `extra_data_paths`) | after the restart cycles the candidate's `faction migrate toJson` / `toDatabase` is exercised both ways (**migration-roundtrip**) |

The baseline's very first boot always runs on the plugin's shipped defaults (the config file
does not exist before it), so on `mariadb`/`postgres`/`json` an untouched default H2 file from
that boot sits in the server root; it is captured with `extra_data_paths` and reported as
kept, unchanged. A supplied fixture combines with every backend: it is unpacked before the
baseline boots, its `db-dump.sql` (if any) is restored into the database backend, and the
baseline's restart over it fixes the reference counts that the cycles and the round trip
are then held to.

### Supplied fixtures

A scenario proves the shapes it was written to create. A fixture published by
[plugin-fixtures](https://github.com/Dans-Plugins/plugin-fixtures) — in particular a
*real-world* one, an anonymised database from a live server — proves the shapes a community
actually produced. With `fixture_url` the gate records nothing: the archive is unpacked into
the server root before the baseline ever runs, so the baseline's first boot is a boot over
that data (proof that the fixture still loads on the current stable), the baseline's restart
fixes the reference counts, those must equal the manifest's `expected`, and the candidate is
then held to both. `config_overrides`, `scenario` and `extra_data_paths` still apply on top —
the scenario runs against the fixture's data, and `extra_data_paths` must still name any
database file that sits outside `plugins/<Name>/`. `result.json` gains
`fixture: {url, manifestUrl, sha256, kind, plugin, version, minecraft, paths, expected}` and
the job summary a `manifest expected` column.

The archive's entries are server-root-relative (`medieval_factions_db.mv.db`,
`plugins/MedievalFactions/config.yml`); an archive whose entries all sit under one wrapper
directory that is not itself a data path — the gate's own `fixture.tar.gz` evidence — is
unwrapped, so a previous run's fixture can be replayed. Symbolic links and paths that escape
the server root are refused.

```
gh workflow run save-compat.yml --repo Dans-Plugins/release-gates \
  -f repository=Dans-Plugins/Medieval-Factions \
  -f sha=<commit> \
  -f jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/dev/<jar> \
  -f baseline_jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/v5.8.1/<jar> \
  -f fixture_url=https://github.com/Dans-Plugins/plugin-fixtures/releases/download/medieval-factions/5.8.1-real/medieval-factions-5.8.1-real.tar.gz \
  -f extra_data_paths='medieval_factions_db*' \
  -f expected_version=6.0.0
```

Dispatch by hand:

```
gh workflow run save-compat.yml --repo Dans-Plugins/release-gates \
  -f repository=Dans-Plugins/Medieval-Factions \
  -f sha=<commit> \
  -f jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/dev/<jar> \
  -f baseline_jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/v5.8.1/<jar> \
  -f config_overrides='factions.allowLeaderlessFactions: true' \
  -f scenario=$'faction admin create Alpha\nfaction admin create Bravo' \
  -f extra_data_paths='medieval_factions_db*' \
  -f restart_cycles=5 \
  -f backend=mariadb
```

One dispatch per backend (`h2`, `mariadb`, `postgres`, `json`) is the full matrix; the runs
are independent and concurrent (the concurrency group includes the backend).

With bots instead of console-created factions (two factions, five claims, an alliance, a
locked chest and a gate — see the script for the sequence):

```
gh workflow run save-compat.yml --repo Dans-Plugins/release-gates \
  -f repository=Dans-Plugins/Medieval-Factions \
  -f sha=<commit> \
  -f jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/dev/<jar> \
  -f baseline_jar_url=https://github.com/Dans-Plugins/Medieval-Factions/releases/download/v5.8.1/<jar> \
  -f expected_version=6.0.0 \
  -f minecraft_version=26.1 \
  -f extra_data_paths='medieval_factions_db*' \
  -f scenario_script=https://raw.githubusercontent.com/Dans-Plugins/plugin-fixtures/main/scenarios/medieval-factions.js
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
| `baseline_jar_url` | no | the current stable jar; when given, every dependent is first booted against it (a **control** phase). A dependent that already fails there is reported as pre-existing and does not fail the gate; only a dependent that enables against the stable and not against the candidate — a regression — does. Trace files the stable leaves are attributed to it, not the candidate. |
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
  `{gate, repository, sha, plugin, baselineVersion, version, backend, restartCycles, passed, counts: {label: [baseline, candidate boot 1, …, candidate boot restartCycles + 1]}, fixture (null, or the supplied fixture's url, manifestUrl, sha256, kind, plugin, version, minecraft, paths, expected, dbDump), fixtureFiles, assertions}`
  (plus `scenarioScript` and `scenarioExpected` when a bot scenario ran)
  plus `fixture.tar.gz` (what the baseline wrote — on a database backend including
  `db-dump.sql`), `fixture-listing.json`, `candidate-data.tar.gz` (the same paths after the
  candidate's last boot), `db.log` (the database container's log), `bot-scenario.log` /
  `bot-scenario.json` (the script's own evidence, with a bot scenario) and, for a `json` run,
  `migrationRoundtrip: {start, legs: [{from, to, migration, storageLine, counts, countsDetail, cleared}]}`
  in the result and `roundtrip-cleared-<store>.tar.gz` (the store emptied before the second leg).
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
