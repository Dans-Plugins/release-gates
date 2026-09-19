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
| [`boot-gate.yml`](.github/workflows/boot-gate.yml) | The candidate enables on a fresh server with its dependencies' current stable releases, stops cleanly, and enables again over the data folder it wrote | available |
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
5. **stop-1** — the server stops within two minutes with no error attributable to the plugin.
6. **boot-2**, **plugins-2**, **stop-2** — the same, booted over the data folder the first boot created. A plugin that writes a file it cannot read back fails here.

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

## Evidence

Every run uploads an artifact `boot-gate-<run id>` containing `result.json`
(`{gate, repository, sha, plugin, version, passed, assertions: [{name, passed, detail}]}`),
`server.log` (the full console), and the plugin's data folder. The job summary shows the
assertion table.

## Design notes

- The server is disposable: offline mode, a placeholder operator UUID, no Discord, no agent.
- The OMCSI checkout and Docker image cache are the pattern from
  [Dan's Plugin Manager's integration tests](https://github.com/Dans-Plugins/Dans-Plugin-Manager/blob/main/.github/workflows/integration.yml);
  on a cache hit the Spigot build is skipped and a gate costs about ten minutes.
- Dependencies come from `/releases/latest`, the endpoint DPM's stable channel reads, and the
  Maven shade plugin's `original-*.jar` is skipped when a release carries both.
- The harness reads the console with `docker logs --since`, scoped per boot, so an error from
  the baseline boot cannot be blamed on the candidate.
