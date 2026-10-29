"""
route_patch/patcher.py — Reforge Stage 4 (surgical editor).

Applies the SEARCH/REPLACE blocks a Nemotron model writes, one hunk at a time:

    src/schema.py
    <<<<<<< SEARCH
        @validator("email")
    =======
        @field_validator("email")
        @classmethod
    >>>>>>> REPLACE

For every hunk:
  1. the SEARCH text must exist in the target file EXACTLY ONCE (exact match first; then a
     trailing-whitespace / CRLF tolerant line match; then an indentation-shifted match that
     re-indents REPLACE by the same offset). Zero or several matches -> rejected, file untouched.
  2. the patched file is parsed with ast (Python files) BEFORE anything is written, so a reply
     that breaks syntax never reaches disk or the test suite.
  3. the write is atomic (temp file + os.replace) and the original text is kept for rollback.

A reply touching several files is applied as one transaction: one bad hunk rolls back the lot.

Entry points:
    parse_blocks(text, default_file=None) -> list[Block]
    apply_blocks(ws, blocks, *, allowed=None, dry_run=False) -> PatchResult
    rollback(ws, result)
"""
from __future__ import annotations

import ast
import difflib
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

CLEAN = "Hunk applied cleanly"
FUZZY_WS = "Hunk applied (whitespace-tolerant match)"
FUZZY_INDENT = "Hunk applied (indentation-shifted match)"
NO_BLOCKS = "No SEARCH/REPLACE block in reply"
NOT_FOUND = "SEARCH block not found in file"
AMBIGUOUS = "SEARCH block is ambiguous"
SYNTAX = "Syntax error after patch"
FORBIDDEN = "File not allowed for patching"
MISSING = "Target file does not exist"
NOOP = "SEARCH and REPLACE are identical"
PLACEHOLDER = "SEARCH copied the prompt's example, not the file"
_EXAMPLE_MARKERS = ("<exact contiguous lines", "old_api(a, b)", "<replacement lines>")
OK_STATUSES = (CLEAN, FUZZY_WS, FUZZY_INDENT)

_BLOCK = re.compile(
    r"(?:^[ \t]*(?:#+\s*|File:\s*|\*\*)?(?P<path>[\w./\\-]+\.\w+)(?:\*\*)?[ \t]*\n)?"   # optional path line
    r"(?:^[ \t]*```[\w+-]*[ \t]*\n)?"                                                 # optional fence
    r"^[ \t]*<{5,9} ?SEARCH[^\n]*\n"
    r"(?P<search>.*?)"
    r"^[ \t]*={5,9}[ \t]*\n"
    r"(?P<replace>.*?)"
    # end marker; a model that forgets `>>>>>>> REPLACE` still ends the block at the next SEARCH / end of reply
    r"(?:^[ \t]*>{5,9} ?REPLACE[^\n]*$|(?=^[ \t]*(?:[\w./\\-]+\.\w+[ \t]*\n[ \t]*)?<{5,9} ?SEARCH)|\Z)",
    re.M | re.S,
)


@dataclass
class Block:
    file: str | None
    search: str
    replace: str


@dataclass
class Hunk:
    file: str
    status: str
    diff: str = ""
    error: str | None = None
    line: int | None = None             # 1-based line where the SEARCH text started

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


@dataclass
class PatchResult:
    status: str
    diff: str
    applied: bool
    hunks: list[Hunk] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    error: str | None = None
    originals: dict[str, str] = field(default_factory=dict, repr=False)   # rel -> text before patch

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("originals", None)
        return d


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)


def parse_blocks(text: str, default_file: str | None = None) -> list[Block]:
    """Every SEARCH/REPLACE block in a model reply. A block without its own path line inherits
    the previous block's path, then `default_file`."""
    blocks, last = [], default_file
    for m in _BLOCK.finditer(_strip_think(text)):
        path = (m.group("path") or "").replace("\\", "/").strip() or None
        if path and not re.search(r"\.(py|txt|toml|cfg|ini|ya?ml|json)$", path):
            path = None
        if path and path.startswith("path/to/"):      # model copied the prompt's placeholder
            path = None
        path = path or last
        blocks.append(Block(file=path, search=m.group("search"), replace=m.group("replace")))
        last = path
    return blocks


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _norm(line: str) -> str:
    return line.rstrip("\r\n").rstrip()


def _offsets(hay: list[str]) -> list[int]:
    out, pos = [], 0
    for line in hay:
        out.append(pos)
        pos += len(line)
    out.append(pos)
    return out


def _indent(s: str) -> str:
    return s[:len(s) - len(s.lstrip())]


def _reindent(text: str, old: str, new: str) -> str:
    out = []
    for line in _lines(text):
        if line.strip() and line.startswith(old):
            line = new + line[len(old):]
        elif line.strip() and old == "":
            line = new + line
        out.append(line)
    return "".join(out)


def locate(source: str, search: str) -> tuple[str, list[tuple[int, int]], str | None]:
    """(match kind, [(start, end) char spans], reindent hint 'old->new' for indent matches)."""
    if not search.strip():
        return NOT_FOUND, [], None
    # 1. exact substring
    spans, i = [], source.find(search)
    while i != -1:
        spans.append((i, i + len(search)))
        i = source.find(search, i + 1)
    if spans:
        return CLEAN, spans, None

    hay = _lines(source)
    needle = [l for l in _lines(search)]
    while needle and not needle[-1].strip():       # trailing blank lines are noise
        needle.pop()
    while needle and not needle[0].strip():
        needle.pop(0)
    if not needle:
        return NOT_FOUND, [], None
    offs, n = _offsets(hay), len(needle)
    want = [_norm(l) for l in needle]

    # 2. line match ignoring trailing whitespace / line endings
    spans = [(offs[s], offs[s + n]) for s in range(len(hay) - n + 1)
             if [_norm(l) for l in hay[s:s + n]] == want]
    if spans:
        return FUZZY_WS, spans, None

    # 3. same lines, uniformly shifted indentation (model dropped / added leading spaces)
    ref = next(l for l in needle if l.strip())
    stripped = [w.lstrip() for w in want]
    hits, hint = [], None
    for s in range(len(hay) - n + 1):
        window = [_norm(l) for l in hay[s:s + n]]
        if [w.lstrip() for w in window] != stripped:
            continue
        first = next(w for w in window if w.strip())
        old, new = _indent(ref), _indent(first)
        if all((not w.strip()) or (_indent(w) == new + _indent(x)[len(old):] if _indent(x).startswith(old)
                                   else False) for w, x in zip(window, [_norm(l) for l in needle])):
            hits.append((offs[s], offs[s + n]))
            hint = f"{old}\x00{new}"
    if hits:
        return FUZZY_INDENT, hits, hint
    return NOT_FOUND, [], None


def _syntax_error(path: str, text: str) -> str | None:
    if not path.endswith(".py"):
        return None
    try:
        ast.parse(text, filename=path)
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}" + (f" -> {e.text.strip()}" if e.text else "")
    return None


def make_diff(before: str, after: str) -> str:
    """Compact '-'/'+' line diff (the format shown in the Stage-4 report)."""
    out = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(f"{line[0]} {line[1:]}")
    return "\n".join(out)


def apply_block(source: str, block: Block) -> tuple[str, Hunk]:
    """Pure function: (new source, hunk report). Source is returned unchanged on failure."""
    rel = block.file or "?"
    if block.search == block.replace:
        return source, Hunk(rel, NOOP)
    if any(m in block.search or m in block.replace for m in _EXAMPLE_MARKERS):
        return source, Hunk(rel, PLACEHOLDER, error="copy real lines from the CURRENT file into SEARCH")
    kind, spans, hint = locate(source, block.search)
    if kind == NOT_FOUND:
        return source, Hunk(rel, NOT_FOUND, error=block.search.strip()[:300])
    if len(spans) > 1:
        return source, Hunk(rel, AMBIGUOUS, error=f"{len(spans)} matches; add surrounding lines to make it unique")
    start, end = spans[0]
    replace = block.replace
    if kind == FUZZY_INDENT and hint is not None:
        old, new = hint.split("\x00")
        replace = _reindent(replace, old, new)
    matched = source[start:end]
    if kind != CLEAN:          # keep the file's own line ending + trailing newline shape
        eol = "\r\n" if "\r\n" in matched else "\n"
        replace = replace.replace("\r\n", "\n").rstrip("\n")
        replace = replace.replace("\n", eol) + (eol if matched.endswith(("\n", "\r\n")) else "")
    new_source = source[:start] + replace + source[end:]
    line = source.count("\n", 0, start) + 1
    return new_source, Hunk(rel, kind, diff=make_diff(matched, replace), line=line)


# --------------------------------------------------------------------------- #
# File-level transaction
# --------------------------------------------------------------------------- #
def _resolve(ws: Path, rel: str) -> Path | None:
    p = (ws / rel).resolve()
    try:
        p.relative_to(ws)
    except ValueError:
        return None          # ../ escape
    return p


def _read(p: Path) -> str:
    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:   # keep CRLF as-is
        return f.read()


def _atomic_write(p: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".reforge")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply_blocks(ws: str | Path, blocks: list[Block], *, allowed: set[str] | None = None,
                 default_file: str | None = None, dry_run: bool = False) -> PatchResult:
    """Apply every block or none. `allowed` = repo-relative paths the caller lets the model edit."""
    ws = Path(ws).resolve()
    if not blocks:
        return PatchResult(status=NO_BLOCKS, diff="", applied=False, error=NO_BLOCKS)

    texts: dict[str, str] = {}
    originals: dict[str, str] = {}
    hunks: list[Hunk] = []

    for b in blocks:
        rel = (b.file or default_file or "").replace("\\", "/").lstrip("./") if (b.file or default_file) else ""
        b = Block(rel, b.search, b.replace)
        if not rel or (allowed is not None and rel not in allowed):
            hunks.append(Hunk(rel or "?", FORBIDDEN, error=f"allowed: {sorted(allowed or [])}"))
            break
        p = _resolve(ws, rel)
        if p is None or not p.is_file():
            hunks.append(Hunk(rel, MISSING))
            break
        if rel not in texts:
            texts[rel] = originals[rel] = _read(p)
        new, hunk = apply_block(texts[rel], b)
        hunks.append(hunk)
        if not hunk.ok:
            break
        texts[rel] = new

    bad = next((h for h in hunks if not h.ok), None)
    if bad is None:                                   # syntax gate before anything touches disk
        for rel, text in texts.items():
            err = _syntax_error(rel, text)
            if err:
                bad = Hunk(rel, SYNTAX, error=err)
                hunks.append(bad)
                break

    diff = "\n".join(h.diff for h in hunks if h.ok and h.diff)
    files = list(texts)
    if bad is not None:
        status = bad.status if bad.status != NOOP else NOOP
        return PatchResult(status=status, diff=diff, applied=False, hunks=hunks, files=files,
                           error=f"{bad.file}: {bad.status}" + (f" ({bad.error})" if bad.error else ""))

    if not dry_run:
        written: list[str] = []
        try:
            for rel, text in texts.items():
                if text != originals[rel]:
                    _atomic_write(_resolve(ws, rel), text)
                    written.append(rel)
        except OSError as e:
            for rel in written:
                _atomic_write(_resolve(ws, rel), originals[rel])
            return PatchResult(status=f"Write failed: {e}", diff=diff, applied=False, hunks=hunks,
                               files=files, error=str(e))

    status = CLEAN if all(h.status == CLEAN for h in hunks) else next(h.status for h in hunks if h.status != CLEAN)
    if len(hunks) > 1:
        status = status.replace("Hunk", f"{len(hunks)} hunks", 1)
    return PatchResult(status=status, diff=diff, applied=not dry_run, hunks=hunks, files=files,
                       originals=originals)


def apply_reply(ws: str | Path, reply: str, *, default_file: str | None = None,
                allowed: set[str] | None = None, dry_run: bool = False) -> PatchResult:
    return apply_blocks(ws, parse_blocks(reply, default_file), allowed=allowed,
                        default_file=default_file, dry_run=dry_run)


def rollback(ws: str | Path, result: PatchResult) -> None:
    ws = Path(ws).resolve()
    for rel, text in result.originals.items():
        p = _resolve(ws, rel)
        if p is not None:
            _atomic_write(p, text)


def syntax_check(ws: str | Path, rel: str) -> str | None:
    p = _resolve(Path(ws).resolve(), rel)
    return _syntax_error(rel, _read(p)) if p and p.is_file() else None
