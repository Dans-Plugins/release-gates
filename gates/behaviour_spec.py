"""Spec mode for the behaviour gate (Stephenson-Software RFC 0019): rows carry what the docs promise.

A row's optional `expect` is `{effect, refusal?, source, reviewed, note?}`:

  effect    whether the action takes effect: any positive observation other than messages, or, for
            a row that observes only messages, that no refusal was sent (the page's notion)
  refusal   a lang key that must be among the refusals; null for "no refusal at all"; absent for
            "don't care"
  source    `<owner>/<repo>/<path>@<sha>#L<a>[-L<b>]`, the documentation that promises it, pinned
            to a commit (the owner is always spelled out: a path may itself contain slashes)
  reviewed  only reviewed expectations are enforced; a reviewed expectation needs a source

Per row and jar the check is `ok`, `mismatch` or `unchecked` (row not observed). Across both jars a
row is `ok`; `mismatch` (candidate only — the one finding that can fail the gate); `pre-existing`
(both jars; never blocks, owner decision 2026-10-05); `fixed` (stable mismatched, candidate
matches); `source-missing` (the cited file or lines do not exist; not enforced); or `unchecked`.
"""

import re

import requests

SOURCE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?P<path>[^@]+)@(?P<sha>[0-9a-f]{7,40})#L(?P<a>\d+)(?:-L(?P<b>\d+))?$")
_SOURCE_CACHE = {}


def parse_source(source):
    """(owner, repo, path, sha, first line, last line), or None when the form is wrong."""
    m = SOURCE.match(source or "")
    if not m:
        return None
    owner, repo, path = m.group("owner"), m.group("repo"), m.group("path")
    a = int(m.group("a"))
    b = int(m.group("b") or a)
    return owner, repo, path, m.group("sha"), a, b


def source_exists(source, fetch=None):
    """True when the cited file exists at the commit and has the cited lines."""
    parsed = parse_source(source)
    if not parsed:
        return False
    owner, repo, path, sha, a, b = parsed
    key = (owner, repo, path, sha)
    if key not in _SOURCE_CACHE:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}"
        try:
            text = fetch(url) if fetch else _get(url)
        except Exception:
            text = None
        _SOURCE_CACHE[key] = text
    text = _SOURCE_CACHE[key]
    return text is not None and len(text.splitlines()) >= b and a <= b


def _get(url):
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        return None
    return r.text


def refusals(outcome, table):
    """The row's refusal keys, without timer-driven messages and bypass notices."""
    ignored = set(table.get("ignoreMessageKeys") or []) | set(table.get("informationalMessageKeys") or [])
    return [k for k in (outcome or {}).get("refusal") or [] if k not in ignored and "Bypass" not in k]


def took_effect(outcome, row, table):
    observes = set(row.get("observe") or []) - {"refusal"}
    if not observes:
        return not refusals(outcome, table)
    return any((v is True) or (isinstance(v, list) and v) for k, v in (outcome or {}).items() if k != "refusal")


def check(expect, observed, row, table):
    """ok | mismatch | unchecked for one jar's observed row."""
    if not observed or observed.get("status") != "observed":
        return "unchecked"
    outcome = observed.get("outcome") or {}
    if took_effect(outcome, row, table) != bool(expect.get("effect")):
        return "mismatch"
    if "refusal" in expect:
        got = refusals(outcome, table)
        want = expect["refusal"]
        if want is None and got:
            return "mismatch"
        if want is not None and want not in got:
            return "mismatch"
    return "ok"


def combine(stable, candidate):
    if candidate == "unchecked":
        return "unchecked"
    if candidate == "mismatch":
        return "pre-existing" if stable == "mismatch" else "mismatch"
    return "fixed" if stable == "mismatch" else "ok"


def evaluate(table, stable_rows, candidate_rows, fetch=None):
    """One entry per row with a reviewed expectation: {id, group, expect, stable, candidate, status}."""
    out = []
    for row in table["rows"]:
        exp = row.get("expect")
        if not exp or not exp.get("reviewed"):
            continue
        entry = {"id": row["id"], "group": row["group"], "expect": exp}
        if not source_exists(exp.get("source"), fetch):
            entry.update(stable="unchecked", candidate="unchecked", status="source-missing")
        else:
            s = check(exp, stable_rows.get(row["id"]), row, table)
            c = check(exp, candidate_rows.get(row["id"]), row, table)
            entry.update(stable=s, candidate=c, status=combine(s, c))
        out.append(entry)
    return out


def propose(table, candidate_rows):
    """Expectations for rows that have none, from the candidate's observations, unreviewed."""
    out = {}
    for row in table["rows"]:
        if row.get("expect"):
            continue
        obs = candidate_rows.get(row["id"])
        if not obs or obs.get("status") != "observed":
            continue
        ref = refusals(obs.get("outcome"), table)
        out[row["id"]] = {"effect": took_effect(obs.get("outcome"), row, table),
                          "refusal": ref[0] if ref else None, "reviewed": False}
    return out
