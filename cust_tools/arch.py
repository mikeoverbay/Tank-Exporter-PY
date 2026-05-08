"""arch.py -- TEPY architecture-doc search / index tool.

The single ARCHITECTURE.md grew past 700 lines, and "where does X live"
turned into grep+guess for both humans and Claude.  This script is the
intended FIRST stop:

    python cust_tools/arch.py search "wheel physics"
    python cust_tools/arch.py search "rubber band track"
    python cust_tools/arch.py list
    python cust_tools/arch.py stale

Designed to run before any code grep.  Searches across:

    ARCHITECTURE.md
    README.md
    README_TANK_VIEWER.md
    CHANGELOG.md
    COORDINATE_SYSTEMS.md
    VISUAL_PROCESSED_FORMAT.md
    CLAUDE.md
    docs/**/*.md

Output for `search`: per file, the nearest enclosing `## ` heading +
the matching line(s) with a tiny snippet of context.

Output for `list`: every `## ` and `### ` heading in every doc, file-
grouped, alphabetised by file.

Output for `stale`: for each `.py` filename mentioned in any doc, if
the .py file is newer than the doc that mentions it (mtime), print a
warning -- the doc may have drifted out of date.

Stdlib only -- no external deps.  Runs from anywhere as long as you
pass the project root explicitly or invoke from the project root.
"""
from __future__ import annotations
import argparse
import io
import os
import re
import sys
from pathlib import Path

# Reconfigure stdout to UTF-8 with `replace` errors so Markdown
# files containing en-dashes / Unicode bullets / checkmarks /
# other non-cp1252 chars don't crash the print path on Windows.
# Python 3.7+ lets us swap encoding in place.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace")


# Doc roots searched in order.  Glob-relative to project root.
DOC_GLOBS = [
    "ARCHITECTURE.md",
    "README.md",
    "README_TANK_VIEWER.md",
    "CHANGELOG.md",
    "COORDINATE_SYSTEMS.md",
    "VISUAL_PROCESSED_FORMAT.md",
    "CLAUDE.md",
    "docs/**/*.md",
]

# Source roots scanned for staleness check.  Doc may name any of these.
SRC_GLOBS = [
    "tankExporterPy/**/*.py",
    "cust_tools/*.py",
    "shaders/*.vert",
    "shaders/*.frag",
    "shaders/*.glsl",
]


def find_project_root(start: Path) -> Path:
    """Walk up until we find a directory holding ARCHITECTURE.md."""
    cur = start.resolve()
    for _ in range(10):
        if (cur / "ARCHITECTURE.md").is_file():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise SystemExit(
        "[arch] Cannot locate project root "
        "(no ARCHITECTURE.md found walking up from "
        f"{start.resolve()})"
    )


def _doc_priority(rel: str) -> int:
    """Lower rank = shown first in search results.

    Reference docs (docs/, ARCHITECTURE, COORDINATE_SYSTEMS,
    VISUAL_PROCESSED_FORMAT) come BEFORE narrative docs (README,
    CLAUDE) and CHANGELOG.  CHANGELOG is a journal -- big, dense,
    wildly redundant -- so it's last by design.  The user can
    still scan it but it doesn't drown out a 1-line hit in a
    domain doc.
    """
    if rel.startswith("docs/INDEX.md"):
        return 0
    if rel.startswith("docs/"):
        return 1
    if rel == "ARCHITECTURE.md":
        return 2
    if rel in ("COORDINATE_SYSTEMS.md", "VISUAL_PROCESSED_FORMAT.md"):
        return 3
    if rel in ("README.md", "README_TANK_VIEWER.md", "CLAUDE.md"):
        return 4
    if rel == "CHANGELOG.md":
        return 9
    return 5


def collect_docs(root: Path) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for pat in DOC_GLOBS:
        for p in root.glob(pat):
            if p.is_file() and p not in seen:
                seen.add(p)
                out.append(p)
    out.sort(key=lambda p: (_doc_priority(p.relative_to(root).as_posix()),
                             p.relative_to(root).as_posix()))
    return out


def collect_sources(root: Path) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for pat in SRC_GLOBS:
        for p in root.glob(pat):
            if p.is_file() and p not in seen:
                seen.add(p)
                out.append(p)
    return out


# ---- search -----------------------------------------------------

H_RX     = re.compile(r"^(#{1,6})\s+(.*)$")


def nearest_heading(lines: list[str], idx: int) -> str:
    """Walk backward from `idx` to find the nearest heading line."""
    for i in range(idx, -1, -1):
        m = H_RX.match(lines[i])
        if m:
            level = len(m.group(1))
            return f"{'#' * level} {m.group(2).strip()}"
    return "<top>"


def _phrase_search(rx: re.Pattern, lines: list[str]) -> list[tuple[int, str]]:
    return [(i, ln) for i, ln in enumerate(lines) if rx.search(ln)]


def _token_and_search(tokens: list[re.Pattern],
                       lines: list[str]) -> list[tuple[int, str]]:
    """Return hits for every token IF every token hits the file at
    least once.  The displayed hit set is the union across tokens
    (so the caller sees evidence each token actually matches)."""
    per_token: list[list[tuple[int, str]]] = []
    for rx in tokens:
        hits = [(i, ln) for i, ln in enumerate(lines) if rx.search(ln)]
        if not hits:
            return []
        per_token.append(hits)
    seen: set[int] = set()
    out: list[tuple[int, str]] = []
    for hits in per_token:
        for i, ln in hits:
            if i not in seen:
                seen.add(i)
                out.append((i, ln))
    out.sort(key=lambda t: t[0])
    return out


def cmd_search(root: Path, query: str, max_hits_per_file: int = 6,
               case_insensitive: bool = True) -> int:
    flags = re.IGNORECASE if case_insensitive else 0
    # Build a phrase regex (preferred -- terms adjacent) AND a list of
    # per-token regexes (fallback -- terms anywhere in the same file).
    # Phrase form treats whitespace flexibly: "rubber band" matches
    # "rubber-band", "rubber_band", or "rubber  band".  Token-AND form
    # rescues queries like "wheel physics" where the two words don't
    # appear adjacent but both belong to the same doc.
    is_regex = any(ch in query for ch in r".*+?^$()[]{}|\\")
    if is_regex:
        phrase_pat = query
        tokens: list[re.Pattern] = []
    else:
        words = query.split()
        phrase_pat = r"\W+".join(re.escape(w) for w in words)
        tokens = [re.compile(re.escape(w), flags) for w in words]
    try:
        phrase_rx = re.compile(phrase_pat, flags)
    except re.error as exc:
        print(f"[arch] bad regex: {exc}", file=sys.stderr)
        return 2
    docs = collect_docs(root)
    total_hits = 0
    fallback_used = False
    for d in docs:
        try:
            lines = d.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[arch] skip {d}: {exc}", file=sys.stderr)
            continue
        hits = _phrase_search(phrase_rx, lines)
        match_kind = "phrase"
        if not hits and tokens and len(tokens) > 1:
            hits = _token_and_search(tokens, lines)
            if hits:
                match_kind = "all-tokens"
                fallback_used = True
        if not hits:
            continue
        rel = d.relative_to(root).as_posix()
        kind_tag = f", {match_kind}" if match_kind == "all-tokens" else ""
        print(f"\n=== {rel} ({len(hits)} hit{'s' if len(hits) != 1 else ''}{kind_tag}) ===")
        last_heading = None
        shown = 0
        for i, ln in hits:
            if shown >= max_hits_per_file:
                print(f"  ... +{len(hits) - shown} more")
                break
            heading = nearest_heading(lines, i)
            if heading != last_heading:
                print(f"  [{heading}]")
                last_heading = heading
            print(f"  L{i + 1}: {ln.strip()[:160]}")
            shown += 1
        total_hits += len(hits)
    if total_hits == 0:
        print(f"[arch] no matches for: {query!r}")
        return 1
    if fallback_used:
        print(f"\n[arch] {total_hits} hit(s); some files matched "
              "all-tokens-anywhere (no exact phrase).")
    else:
        print(f"\n[arch] {total_hits} total hit(s)")
    return 0


# ---- list -------------------------------------------------------

def cmd_list(root: Path, max_depth: int = 3) -> int:
    docs = collect_docs(root)
    for d in docs:
        rel = d.relative_to(root).as_posix()
        try:
            lines = d.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        headings = []
        for ln in lines:
            m = H_RX.match(ln)
            if m and len(m.group(1)) <= max_depth:
                headings.append((len(m.group(1)), m.group(2).strip()))
        if not headings:
            continue
        print(f"\n=== {rel} ===")
        for level, text in headings:
            indent = "  " * (level - 1)
            print(f"  {indent}{'#' * level} {text}")
    return 0


# ---- stale ------------------------------------------------------

PY_MENTION_RX = re.compile(r"\b([\w/]+\.(?:py|vert|frag|glsl))\b")


def cmd_stale(root: Path) -> int:
    docs = collect_docs(root)
    sources = collect_sources(root)
    src_index: dict[str, Path] = {}
    for s in sources:
        rel = s.relative_to(root).as_posix()
        src_index[rel] = s
        # Also index by basename so a doc that says `viewer.py`
        # without a path still resolves (preferred match: longest
        # path that ends with the basename; first wins on ties).
        bn = s.name
        src_index.setdefault(bn, s)
    flagged = 0
    print(f"[arch] checking {len(docs)} doc(s) against "
          f"{len(sources)} source file(s)...")
    for d in docs:
        try:
            text = d.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        d_mtime = d.stat().st_mtime
        mentions = set(PY_MENTION_RX.findall(text))
        stale_here: list[tuple[str, float]] = []
        for m in sorted(mentions):
            src = src_index.get(m)
            if src is None:
                continue
            s_mtime = src.stat().st_mtime
            if s_mtime > d_mtime + 1.0:   # 1s slack for FS quirks
                age_h = (s_mtime - d_mtime) / 3600.0
                stale_here.append((m, age_h))
        if stale_here:
            rel = d.relative_to(root).as_posix()
            print(f"\n  {rel}")
            for m, age_h in stale_here:
                if age_h > 24:
                    age_s = f"{age_h / 24:.1f} days newer"
                else:
                    age_s = f"{age_h:.1f} h newer"
                print(f"    - {m} ({age_s})")
            flagged += len(stale_here)
    if flagged == 0:
        print("\n[arch] all referenced sources are <= doc mtimes "
              "(no obvious staleness)")
        return 0
    print(f"\n[arch] {flagged} stale reference(s) flagged.  "
          "Verify the docs above still describe the current code.")
    return 0   # exit 0 -- staleness is informational


# ---- main -------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="arch.py",
        description="TEPY architecture-doc search / index / staleness tool.",
    )
    p.add_argument("--root", type=Path, default=None,
                   help="project root (default: walk up from CWD looking "
                        "for ARCHITECTURE.md)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("search", help="grep docs, group by file + section")
    pp.add_argument("query", help="regex (case-insensitive by default)")
    pp.add_argument("--case", action="store_true",
                    help="case-sensitive match")
    pp.add_argument("--max", type=int, default=6,
                    help="max hits shown per file (default 6)")
    sub.add_parser("list", help="enumerate every doc heading")
    sub.add_parser("stale",
                   help="flag .py files newer than the docs that mention them")
    a = p.parse_args(argv)
    root = (a.root or find_project_root(Path.cwd())).resolve()
    if a.cmd == "search":
        return cmd_search(root, a.query,
                          max_hits_per_file=a.max,
                          case_insensitive=not a.case)
    if a.cmd == "list":
        return cmd_list(root)
    if a.cmd == "stale":
        return cmd_stale(root)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
