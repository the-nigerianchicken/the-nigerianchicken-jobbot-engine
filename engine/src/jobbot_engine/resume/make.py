"""Makes one resume tailored to a posting from a person's kit, with one Claude call for the decisions.

Claude reads the kit's facts (profile.yaml) and rules (PIPELINE.md) with the posting, and answers once with
the decisions: which roles, their bullets ranked by how much they matter here, the project, the skills rows,
and the notes for the person. Code does the rest, as the owner asked (2026-11-13: his Claude use is small):
it writes the LaTeX, fits the page by adding bullets in rank order while it stays one page, spaces the lines
out to fill what's left, and runs the kit's own checker (verify_resume.py, which compiles with tectonic). Text
the checker flags goes back to Claude, at most twice, in a small call with only the bullets.

The kit is the person's, kept in the app and never in this repo:
  profile.yaml      experiences (id, title, company, other company_* names, location, dates), projects (id,
                    name), education.latex, and latex: documentclass, geometry, header, skills_table_open
  PIPELINE.md       their rules for picking and writing
  resume.cls        the LaTeX class
  verify_resume.py  the checker: prints "FAIL: ..." lines and "RESULT: PASS", and leaves the PDF by the .tex
  fit.yaml          file_name ("Name_{company}"), max_roles, bullets ({default: [least, most], <id>: ...}),
                    keep (role ids never dropped), project_bullets, rhythm ([low, high]), preamble (lines
                    after the geometry, with {rhythm}), extras ({name: {when, latex}}: sections Claude
                    may add), checks (the checker's words for a page too long or too short)
"""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml

log = logging.getLogger(__name__)

Ask = Callable[[str], dict[str, Any]]

SYSTEM = (
    "You write the content of one tailored resume. You make decisions only; code builds the LaTeX, "
    "compiles it and fits the page. Answer with one JSON object and nothing else."
)
FIXES = 2
CHECKER = "verify_resume.py"
TOO_LONG = "pages (must be 1)"
TOO_SHORT = "empty space below"
RIGHT_EDGE = "off the right edge"

FIX = """The page was built from your answer and the checker flagged these lines:

{fails}

The bullets now on the page, by rank:
{bullets}

Return one JSON object {{"bullets": [{{"rank": <rank>, "text": "<new text>"}}]}} replacing only the
bullets that need it (same rules as before). Nothing else."""


class Unmade(Exception):
    """The resume couldn't be made, for the reason the message says in plain words."""


def tex_text(text: str) -> str:
    """Claude's plain text, with **bold**, as LaTeX."""
    text = text.replace("\\", "").replace(" — ", ", ").replace("—", ", ").replace("–", "-")
    for c in "&%$#_":
        text = text.replace(c, "\\" + c)
    text = text.replace("~", "\\textasciitilde{}").replace("^", "")
    return re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", text)


def read_answer(output: str) -> dict[str, Any]:
    """The JSON object in the result of `claude -p --output-format json`."""
    text: str = json.loads(output)["result"]
    try:
        answer: dict[str, Any] = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except ValueError:
        raise Unmade("Claude's answer had no JSON in it") from None
    return answer


def ask_claude(prompt: str, model: str = "sonnet") -> dict[str, Any]:
    """One headless Claude Code call with no tools; its usage is logged, never its words."""
    out = subprocess.run(
        [
            "claude",
            "-p",
            "--model",
            model,
            "--tools",
            "",
            "--system-prompt",
            SYSTEM,
            "--output-format",
            "json",
        ],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        check=False,
    )
    if out.returncode != 0:
        raise Unmade(f"Claude didn't answer (exit {out.returncode})")
    usage: dict[str, Any] = json.loads(out.stdout).get("modelUsage", {})
    log.info("Claude's usage: %s", json.dumps(usage))
    return read_answer(out.stdout)


class Limits:
    """fit.yaml, with its defaults."""

    def __init__(self, fit: dict[str, Any], profile: dict[str, Any]) -> None:
        bullets: dict[str, list[int]] = fit.get("bullets") or {}
        self.default: tuple[int, int] = tuple(bullets.get("default", [2, 3]))  # type: ignore[assignment]
        self.bullets = {k: (v[0], v[1]) for k, v in bullets.items() if k != "default"}
        first = profile["experiences"][0]["id"] if profile.get("experiences") else None
        self.keep: set[str] = set(fit.get("keep") or ([first] if first else []))
        self.max_roles: int = fit.get("max_roles", 4)
        self.project_bullets: int = fit.get("project_bullets", 2)
        low, high = fit.get("rhythm", [1.0, 1.0])
        self.rhythm: tuple[float, float] = (float(low), float(high))
        self.preamble: list[str] = fit.get("preamble") or []
        self.extras: dict[str, dict[str, str]] = fit.get("extras") or {}
        self.file_name: str = fit.get("file_name", "Resume_{company}")
        checks: dict[str, str] = fit.get("checks") or {}
        self.too_long = checks.get("too_long", TOO_LONG)
        self.too_short = checks.get("too_short", TOO_SHORT)

    def least(self, role: str) -> int:
        return self.bullets.get(role, self.default)[0]

    def most(self, role: str) -> int:
        return self.bullets.get(role, self.default)[1]


def contract(limits: Limits) -> str:
    most = ", ".join(f"{k} {v[1]}" for k, v in limits.bullets.items())
    least = ", ".join(f"{k} {v[0]}" for k, v in limits.bullets.items())
    extras = "\n".join(f"  - {name}: {e.get('when', '')}" for name, e in limits.extras.items()) or "  (none)"
    return f"""
## What you return (code does the LaTeX, the compiling, the page fitting and the checking)

One JSON object, no prose around it:

{{
  "experience": [{{"id": "<profile experience id>", "org": "company" or another company_* key of it,
                  "bullets": [{{"text": "...", "rank": 1}}]}}],
  "project": {{"id": "<profile project id>", "stack": "Go, Python, ...",
               "bullets": [{{"text": "...", "rank": 9}}]}},
  "extras": ["<name>"],
  "skills": [{{"label": "Languages", "items": "Python, Java, ..."}}],
  "notes": "<markdown: what you picked, every stretched claim labelled, the gaps, and anything that rules
            the person out, such as citizenship or place>"
}}

Rules for the text:
- Plain text. Mark bold with **double asterisks**. No LaTeX, no em dashes, no "--".
- Bold every number and metric in a bullet (**40%**, **3x**, **12,000 records**, **$2M**), and the
  one or two tools the posting asks for most.
- Each bullet is 185 to 215 characters (not counting the asterisks), so it fills two full lines.
- rank orders every bullet on the page by how much it matters for THIS posting: 1 is the last one you'd
  cut. Ranks are unique across the whole page.
- Always include {" and ".join(sorted(limits.keep))}, whatever the posting.
- Pick up to {limits.max_roles} roles and give EACH its most bullets: {most}, any other role
  {limits.default[1]}; the project exactly {limits.project_bullets}. Code fills the page from these by rank,
  keeps each role's least ({least}, others {limits.default[0]}), and drops your weakest role that isn't one
  of {", ".join(sorted(limits.keep))} if they can't all fit.
- Vary the opening verb: no two bullets in one role start with the same word.
- The project stack: at most 6 tools, most relevant first.
- Each skills row fits one line: label plus at most about 75 characters of items. Exactly 4 rows.
- extras: the optional sections to add, by name, when they fit the posting:
{extras}
"""


def prompt_for(work: dict[str, Any]) -> str:
    kit: dict[str, str] = work["kit"]
    posting: dict[str, str] = work["posting"]
    limits = Limits(yaml.safe_load(kit.get("fit.yaml", "")) or {}, yaml.safe_load(kit["profile.yaml"]) or {})
    parts = [
        "# PROFILE (profile.yaml)\n" + kit["profile.yaml"],
        "# RULES (PIPELINE.md)\n" + kit.get("PIPELINE.md", ""),
        contract(limits),
        "# THE POSTING\n"
        + f"{posting['title']} at {posting['company']} ({posting['location']})\n{posting['url']}\n\n"
        + posting["description"],
    ]
    if work.get("note"):
        parts.append("# WHAT THE PERSON ASKED TO CHANGE (this is a redo)\n" + str(work["note"]))
    return "\n\n".join(parts)


def _opener(b: dict[str, Any]) -> str:
    words = str(b["text"]).split()
    return re.sub(r"[^a-z]", "", words[0].lower()) if words else ""


def ordered(bullets: list[dict[str, Any]], keep: set[int]) -> list[dict[str, Any]]:
    """The kept bullets by rank, nudged so no two neighbours open with the same word."""
    left = sorted((b for b in bullets if b["rank"] in keep), key=lambda b: b["rank"])
    out: list[dict[str, Any]] = []
    while left:
        pick = next((b for b in left if not out or _opener(b) != _opener(out[-1])), left[0])
        out.append(pick)
        left.remove(pick)
    return out


def _list(x: Any) -> list[Any]:
    if not isinstance(x, list):
        raise TypeError
    return cast(list[Any], x)


def _bullets(x: Any) -> None:
    for b in _list(x):
        if not isinstance(b["text"], str) or not isinstance(b["rank"], int):
            raise TypeError


def _ids(profile: dict[str, Any], key: str) -> set[str]:
    return {str(x["id"]) for x in cast(list[dict[str, Any]], profile.get(key) or [])}


def check(d: Any, profile: dict[str, Any]) -> None:
    """Claude's decisions have the shape code builds from: roles and a project the profile has, and each
    bullet a rank of its own, since the page is filled and cut by rank."""
    try:
        if not isinstance(d, dict):
            raise TypeError
        answer = cast(dict[str, Any], d)
        for e in _list(answer["experience"]):
            if not isinstance(e["id"], str) or not e["bullets"]:
                raise TypeError
            _bullets(e["bullets"])
        project = answer["project"]
        if not isinstance(project["id"], str) or not isinstance(project["stack"], str):
            raise TypeError
        _bullets(project["bullets"])
        for r in _list(answer["skills"]):
            if not isinstance(r["label"], str) or not isinstance(r["items"], str):
                raise TypeError
        if not isinstance(answer.get("notes") or "", str) or not isinstance(answer.get("extras") or [], list):
            raise TypeError
    except (KeyError, TypeError) as error:
        raise Unmade("Claude's answer wasn't in the shape asked for") from error
    roles = _ids(profile, "experiences")
    projects = _ids(profile, "projects")
    unknown = [e["id"] for e in answer["experience"] if e["id"] not in roles]
    unknown += [project["id"]] if project["id"] not in projects else []
    if unknown or not answer["experience"]:
        raise Unmade(
            f"Claude's answer picked what the profile doesn't have: {', '.join(unknown) or 'no roles'}"
        )
    ranks = [b["rank"] for e in answer["experience"] for b in e["bullets"]]
    ranks += [b["rank"] for b in project["bullets"]]
    if len(ranks) != len(set(ranks)):
        raise Unmade("Claude's answer gave two bullets the same rank, so the page can't be filled by rank")


class Page:
    """One resume being built in `folder` from the kit and Claude's decisions."""

    def __init__(self, work: dict[str, Any], d: dict[str, Any], folder: Path) -> None:
        kit: dict[str, str] = work["kit"]
        self.profile: dict[str, Any] = yaml.safe_load(kit["profile.yaml"]) or {}
        self.limits = Limits(yaml.safe_load(kit.get("fit.yaml", "")) or {}, self.profile)
        self.d = d
        self.folder = folder
        for name, body in kit.items():
            if "/" not in name and "\\" not in name and name not in ("profile.yaml", "PIPELINE.md"):
                (folder / name).write_text(body, encoding="utf-8")
        posting: dict[str, str] = work["posting"]
        # The posting beside the page, for a checker whose rules depend on it.
        head = f"# {posting['title']} at {posting['company']}\n\n{posting['location']}"
        (folder / "JD.md").write_text(f"{head}\n\n{posting['description']}\n", encoding="utf-8")
        company = re.sub(r"[^A-Za-z0-9]", "", posting["company"]) or "Company"
        name = re.sub(r"[^A-Za-z0-9_.-]", "", self.limits.file_name.format(company=company))
        self.name = name[:100] or "Resume"
        self.tex = folder / f"{self.name}.tex"
        self.stack = 6
        self.builds = 0

    def bullets(self) -> list[dict[str, Any]]:
        return [b for e in self.d["experience"] for b in e["bullets"]] + self.d["project"]["bullets"]

    def render(self, rhythm: float, keep: set[int]) -> str:
        lx: dict[str, str] = self.profile["latex"]
        out = [lx["documentclass"], lx["geometry"]]
        out += [line.replace("{rhythm}", f"{rhythm:.2f}") for line in self.limits.preamble]
        out += ["", lx["header"].strip(), "", "\\begin{document}", "", "\\begin{rSection}{Education}"]
        out += [
            str(self.profile["education"]["latex"]).strip(),
            "\\end{rSection}",
            "",
            "\\begin{rSection}{Experience}",
        ]
        chosen = {e["id"]: e for e in self.d["experience"]}
        for exp in self.profile["experiences"]:
            e = chosen.get(exp["id"])
            if not e or not any(b["rank"] in keep for b in e["bullets"]):
                continue
            org_key = str(e.get("org") or "company")
            org = exp.get(org_key) if org_key.startswith("company") else None
            org = org or exp["company"]
            out.append(
                f"\\rEntry{{{exp['title']}}}{{{exp['dates']}}}{{{tex_text(org)}}}{{{exp['location']}}}"
            )
            out.append("\\begin{itemize}")
            out += [f"\\item {tex_text(b['text'])}" for b in ordered(e["bullets"], keep)]
            out += ["\\end{itemize}", ""]
        out[-1:] = ["\\end{rSection}", ""]
        p = self.d["project"]
        name = next((x["name"] for x in self.profile.get("projects", []) if x["id"] == p["id"]), p["id"])
        stack = ", ".join(p["stack"].split(", ")[: self.stack])
        out += [
            "\\begin{rSection}{Projects}",
            f"\\rProject{{{tex_text(name)}}}{{{tex_text(stack)}}}",
            "\\begin{itemize}",
        ]
        every = {b["rank"] for b in p["bullets"]}
        out += [f"\\item {tex_text(b['text'])}" for b in ordered(p["bullets"], every)]
        out += ["\\end{itemize}", "\\end{rSection}", ""]
        extras: list[Any] = self.d.get("extras") or []
        for extra in extras:
            if extra in self.limits.extras:
                out += [self.limits.extras[extra]["latex"].strip(), ""]
        out += ["\\begin{rSection}{Skills}", lx["skills_table_open"]]
        out += [f"\\textbf{{{tex_text(r['label'])}}} & {tex_text(r['items'])} \\\\" for r in self.d["skills"]]
        out += ["\\end{tabular}", "\\end{rSection}", "", "\\end{document}", ""]
        return "\n".join(out)

    def build(self, keep: set[int], rhythm: float) -> tuple[bool, list[str]]:
        self.builds += 1
        self.last = (frozenset(keep), rhythm)
        self.tex.write_text(self.render(rhythm, keep), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, CHECKER, self.tex.name],
            cwd=self.folder,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        lines = (r.stdout + r.stderr).splitlines()
        fails = [x for x in lines if x.startswith("FAIL")]
        return any("RESULT: PASS" in x for x in lines), fails

    def over(self, fails: list[str]) -> bool:
        return any(self.limits.too_long in f for f in fails)

    def fit(self) -> tuple[bool, list[str]]:
        """Each role's least, dropping the weakest droppable role while even that's too long; then the rest
        by rank while it stays one page; then the line spacing up while there's room."""
        lim = self.limits
        best = {e["id"]: min(b["rank"] for b in e["bullets"]) for e in self.d["experience"]}
        roles = sorted(self.d["experience"], key=lambda e: (e["id"] not in lim.keep, best[e["id"]]))
        roles = roles[: lim.max_roles]
        low, high = lim.rhythm
        while True:
            keep = {
                b["rank"]
                for e in roles
                for b in sorted(e["bullets"], key=lambda b: b["rank"])[: lim.least(e["id"])]
            }
            ok, fails = self.build(keep, low)
            droppable = [e for e in roles if e["id"] not in lim.keep]
            if self.over(fails) and droppable:
                roles.remove(droppable[-1])
                continue
            break
        if not self.over(fails):
            spare = sorted((b["rank"], e["id"]) for e in roles for b in e["bullets"] if b["rank"] not in keep)
            for rank, role in spare:
                taken = sum(1 for e in roles if e["id"] == role for b in e["bullets"] if b["rank"] in keep)
                if taken >= lim.most(role):
                    continue
                ok2, fails2 = self.build(keep | {rank}, low)
                if not self.over(fails2):
                    keep.add(rank)
                    ok, fails = ok2, fails2
            rhythm = low
            while not ok and any(lim.too_short in f for f in fails) and rhythm + 0.025 < high:
                ok2, fails2 = self.build(keep, round(rhythm + 0.05, 2))
                if self.over(fails2):
                    break
                rhythm = round(rhythm + 0.05, 2)
                ok, fails = ok2, fails2
            # The folder holds the last page tried, which may be one that didn't fit: build the chosen one.
            if self.last != (frozenset(keep), rhythm):
                ok, fails = self.build(keep, rhythm)
        self.kept = keep
        self.d["experience"] = [e for e in self.d["experience"] if e in roles]
        return ok, fails


def make(work: dict[str, Any], ask: Ask, folder: Path) -> dict[str, Any]:
    """The resume-answer record for one resume: done with its PDF, or failed in plain words."""
    try:
        d = ask(prompt_for(work))
        page = Page(work, d, folder)
        check(d, page.profile)
        ok, fails = page.fit()
        fixes = 0
        while not ok and fixes < FIXES:
            stack_line = d["project"]["stack"].split(", ")[0]
            if page.stack > 3 and any(RIGHT_EDGE in f and stack_line in f for f in fails):
                page.stack -= 1
                ok, fails = page.fit()
                continue
            text = [f for f in fails if page.limits.too_short not in f and page.limits.too_long not in f]
            if not text:
                break
            fixes += 1
            live = "\n".join(
                f"{b['rank']}: {b['text']}" for b in sorted(page.bullets(), key=lambda b: b["rank"])
            )
            answer = ask(FIX.format(fails="\n".join(text), bullets=live))
            new: dict[Any, Any] = {}
            for b in _list(answer.get("bullets", [])):
                if isinstance(b, dict):
                    fix = cast(dict[str, Any], b)
                    if "rank" in fix and isinstance(fix.get("text"), str):
                        new[fix["rank"]] = fix["text"]
            for b in page.bullets():
                b["text"] = new.get(b["rank"], b["text"])
            ok, fails = page.fit()
        log.info(
            "Resume %s: %s after %d builds and %d fixes",
            work["id"],
            "PASS" if ok else fails,
            page.builds,
            fixes,
        )
        if not ok:
            if page.over(fails):
                raise Unmade("It didn't fit on one page, even with the fewest bullets the rules allow")
            raise Unmade(
                "The checker still flagged it: " + "; ".join(f.removeprefix("FAIL: ") for f in fails)[:400]
            )
        pdf = page.tex.with_suffix(".pdf").read_bytes()
    except Unmade as error:
        return {"status": "failed", "file_name": None, "pdf": None, "notes": None, "error": str(error)[:500]}
    except (subprocess.SubprocessError, OSError, yaml.YAMLError, KeyError, ValueError) as error:
        log.exception("Resume %s went wrong", work["id"])
        why = f"Making it went wrong ({type(error).__name__})"
        return {"status": "failed", "file_name": None, "pdf": None, "notes": None, "error": why}
    return {
        "status": "done",
        "file_name": page.name,
        "pdf": base64.b64encode(pdf).decode("ascii"),
        "notes": str(d.get("notes") or "")[:20000] or None,
        "error": None,
    }
