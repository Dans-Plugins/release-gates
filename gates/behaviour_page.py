"""Render a behaviour gate run as a "who can do what" page (Stephenson-Software RFC 0017).

The gate observes, for every row of a behaviour table, what one role managed to do in the world.
Laid out as one table per config group and arena, with an action per line and a role per column,
that is the plugin's protection behaviour as it really is, which is worth publishing beside the
documentation that describes it. The page is written to the run's evidence as `behaviour.md`.

Cells: ✅ the action took effect; ❌ it was refused; ✅⚠ it took effect but the player was also
told it was refused (the shape of Medieval-Factions#2010); · not observed (the bot could not
decide the row). A row that observes only messages shows ❌ when a refusal was sent, ✅ otherwise.
Messages that tell a player they bypassed a protection (any lang key containing "Bypass", or one
listed in the table's `informationalMessageKeys`) are notices, not refusals, and never set ⚠.
"""


def _target(t):
    if "block" in t:
        return t["block"].replace("_", " ")
    if "floor" in t:
        return t["floor"].replace("_", " ")
    if "entity" in t:
        return t["entity"].replace("_", " ")
    return "?"


def action_label(row):
    """A short human description of a row's action, without its role."""
    item = (row.get("item") or "").replace("_", " ")
    target = _target(row.get("target") or {})
    if row["action"] == "attackPlayer":
        return f"hit the {row.get('targetRole')}"
    if row["action"] == "breakBlock":
        text = f"break {target}"
    elif row["action"] == "useOnEntity":
        text = f"use {item} on {target}" if item else f"right-click {target}"
    elif item:
        text = f"{item} on {target}"
        if row.get("count") == 1 and row.get("alsoUseItem"):
            text += " (last item)"
    else:
        text = f"right-click {target}"
    if row.get("sneak"):
        text = "sneaking: " + text
    return text


def _took_effect(outcome):
    return any((v is True) or (isinstance(v, list) and v) for k, v in outcome.items() if k != "refusal")


def _refusals(outcome, informational):
    return [k for k in outcome.get("refusal") or [] if "Bypass" not in k and k not in informational]


def cell(observed, row, informational=()):
    if observed is None or observed.get("status") != "observed":
        return "·"
    outcome = observed.get("outcome") or {}
    refused_msg = bool(_refusals(outcome, set(informational)))
    if not (set(row.get("observe") or []) - {"refusal"}):
        return "❌" if refused_msg else "✅"
    if _took_effect(outcome):
        return "✅⚠" if refused_msg else "✅"
    return "❌"


def render(table, docs, version, roles_order=None):
    """`docs` maps config group → the driver's outcome document for that group (rows with id,
    role, arena, status, outcome). Returns Markdown."""
    by_id = {r["id"]: r for r in table["rows"]}
    observed = {}
    for doc in docs.values():
        for r in (doc or {}).get("rows", []):
            observed[r["id"]] = r
    lines = [f"# Who can do what — {table.get('plugin')} {version}", "",
             "Observed by bots on a real server (the behaviour gate, Stephenson-Software RFC 0017), not written by "
             "hand. ✅ took effect · ❌ refused · ✅⚠ took effect but the player was told it was refused · · not observed.", ""]
    for group, overrides in table["configGroups"].items():
        rows = [r for r in table["rows"] if r["group"] == group]
        if not rows:
            continue
        for arena in dict.fromkeys(r["arena"] for r in rows):
            arena_rows = [r for r in rows if r["arena"] == arena]
            roles = list(dict.fromkeys(r["role"] for r in arena_rows))
            if roles_order:
                roles = [x for x in roles_order if x in roles] + [x for x in roles if x not in roles_order]
            actions = list(dict.fromkeys(action_label(r) for r in arena_rows))
            setting = ", ".join(f"`{k}: {v}`" for k, v in overrides.items()) or "default config"
            owner = (table.get("arenas", {}).get(arena) or {}).get("owner")
            where = f"{arena} ({'claimed by ' + owner if owner else 'wilderness'})"
            lines += [f"## {where} — {setting}", "", "| action | " + " | ".join(roles) + " |",
                      "|---|" + "---|" * len(roles)]
            for a in actions:
                cells = []
                for role in roles:
                    match = next((r for r in arena_rows if r["role"] == role and action_label(r) == a), None)
                    cells.append(cell(observed.get(match["id"]), match, table.get("informationalMessageKeys") or ())
                                 if match else "")
                lines.append(f"| {a} | " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines)
