"""Render the README screenshots' source pages from real gate evidence.

Each gate's job summary is produced by running that workflow's own `Summarize` step (the Python
heredoc, extracted from the workflow file) over a `result.json` downloaded from a real run, then
rendered from Markdown to a plain HTML page. The behaviour page is a real run's `behaviour.md`.
The API-compatibility pages show the captured output of `gates/check_api_compat.py`.

    python3 docs/screenshots/render.py          # writes work/screenshot-pages/*.html
    darkroom shoot docs/screenshots/media.json  # screenshots them into docs/screenshots/
"""
import html
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EVIDENCE = os.path.join(HERE, "evidence")
OUT = os.path.join(ROOT, "work", "screenshot-pages")

SUMMARIES = [
    # (page name, workflow, evidence file, run id)
    ("boot-gate-summary", "boot-gate.yml", "boot-gate-44865257846.json", "44865257846"),
    ("save-compat-summary", "save-compat.yml", "save-compat-44864006184.json", "44864006184"),
    ("save-compat-failed-summary", "save-compat.yml", "save-compat-44864933584.json", "44864933584"),
    ("dependents-summary", "dependents.yml", "dependents-44859771279.json", "44859771279"),
]
TERMINALS = [
    ("api-compat-pass", "api-compat-medieval-factions-11.1.0.txt", 0),
    ("api-compat-fail", "api-compat-medieval-factions-6.0.0.txt", 1),
]
COMMAND = "python3 check_api_compat.py --versions minecraft-versions.json target build/libs"

CSS = """
body{margin:0;background:#fff;color:#1f2328;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
Helvetica,Arial,sans-serif}
main{padding:20px 24px}
h1{font-size:22px;border-bottom:1px solid #d1d9e0;padding-bottom:6px;margin:0 0 12px}
h2{font-size:18px;margin:18px 0 10px}
p{margin:0 0 12px}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:#eff1f3;padding:2px 5px;
border-radius:5px}
table{border-collapse:collapse;margin:0 0 16px;font-size:13px}
th,td{border:1px solid #d1d9e0;padding:5px 10px;text-align:left;vertical-align:top}
th{background:#f6f8fa;font-weight:600}
tr:nth-child(2n) td{background:#f6f8fa}
.note{color:#59636e;font-size:12px;margin-top:4px}
"""

TERM_CSS = """
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,
Consolas,monospace}
pre{margin:0;padding:18px 22px;white-space:pre-wrap}
.p{color:#7ee787}.ok{color:#7ee787}.bad{color:#ff7b72}.dim{color:#8b949e}
"""


def summarize_code(workflow):
    """The Python heredoc of the workflow's `Summarize` step, dedented."""
    text = open(os.path.join(ROOT, ".github", "workflows", workflow)).read()
    m = re.search(r"- name: Summarize\n.*?<<'EOF'.*?\n(.*?)\n\s*EOF\n", text, re.S)
    lines = m.group(1).split("\n")
    indent = min(len(l) - len(l.lstrip()) for l in lines if l.strip())
    return "\n".join(l[indent:] for l in lines)


def run_summary(workflow, result_path):
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "work"))
        with open(result_path) as src, open(os.path.join(tmp, "work", "result.json"), "w") as dst:
            dst.write(src.read())
        return subprocess.run([sys.executable, "-c", summarize_code(workflow)], cwd=tmp,
                              capture_output=True, text=True, check=True).stdout


def inline(text):
    parts = re.split(r"(`[^`]*`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            out.append(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(part)))
    return "".join(out)


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_html(md):
    """Enough Markdown for job summaries and behaviour.md: headings, paragraphs, pipe tables."""
    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")
        elif line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[-| ]+\|$", lines[i + 1]):
            out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells(line)) + "</tr>")
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append("</table>")
            continue
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
        i += 1
    return "\n".join(out)


def page(title, body, css):
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{css}</style></head><body>{body}</body></html>")


def write(name, text):
    with open(os.path.join(OUT, name + ".html"), "w") as f:
        f.write(text)
    print(os.path.relpath(os.path.join(OUT, name + ".html"), ROOT))


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, workflow, evidence, run in SUMMARIES:
        md = run_summary(workflow, os.path.join(EVIDENCE, evidence))
        note = (f"<p class='note'>Job summary written by the <code>Summarize</code> step of "
                f"<code>{workflow}</code> from run {run}'s <code>result.json</code>.</p>")
        write(name, page(name, f"<main>{markdown_to_html(md)}{note}</main>", CSS))
    md = open(os.path.join(EVIDENCE, "behaviour-44859975781.md")).read()
    write("behaviour-page", page("behaviour", f"<main>{markdown_to_html(md)}</main>", CSS))
    for name, evidence, code in TERMINALS:
        lines = [f"<span class='p'>$</span> {html.escape(COMMAND)}"]
        for line in open(os.path.join(EVIDENCE, evidence)).read().rstrip("\n").split("\n"):
            cls = ("bad" if re.search(r"problem\(s\)|does not exist|is called as", line)
                   else "ok" if "every Bukkit reference resolves" in line else "")
            lines.append(f"<span class='{cls}'>{html.escape(line)}</span>" if cls else html.escape(line))
        lines.append(f"<span class='p'>$</span> echo $?\n{code}")
        write(name, page(name, "<pre>" + "\n".join(lines) + "</pre>", TERM_CSS))


if __name__ == "__main__":
    main()
