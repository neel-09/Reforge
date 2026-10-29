"""
audit/report.py — Reforge Stage 5 (Audit Report Compiler): one downloadable PDF for the executive.

Inputs: the pipeline result (scan / baseline / stage2 / stage3 / stage4, i.e. the /api/run-pipeline
response) + the parity result from audit/verifier.py (run on demand when not supplied).

data/audit/<ws>/
    report.pdf            executive report:
                            1. executive summary + KPI tiles + verdict
                            2. security vulnerabilities resolved (OSV, re-checked against the new version)
                            3. official documentation used (grounding sources, per symbol / plan step)
                            4. performance matrix: test pass rate, output parity, memory, latency (v1 vs v2)
                            5. AI cost ledger: tokens + $ per model, Ultra-only counterfactual, money saved
                            6. code diffs: unified + side-by-side for every refactored file
    reforge.patch         all refactored files, git-style unified diff (`git apply` ready)
    diffs/<file>.patch    one unified diff per file
    diffs/<file>.html     one side-by-side diff per file
    cost_ledger.csv       every LLM dispatch of this run

Entry points:
    build_report(ws_path, pipeline=None, parity=None, *, run_parity=True, recheck_cves=True) -> dict
    run_stage5(ws_path, pipeline) -> dict          # verifier + report, for the endpoint
    python -m src.backend.audit.report workspace/<repo> pipeline.json [--parity parity.json]

Needs: reportlab (pip install reportlab)
"""
from __future__ import annotations

import argparse
import csv
import difflib
import html
import json
import re
import textwrap
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Flowable, CondPageBreak, Frame, KeepTogether, NextPageTemplate, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)
from reportlab.pdfbase.pdfmetrics import stringWidth

from src.backend.audit.verifier import AUDIT_DIR, _backup, refactored_files, run_verifier
from src.backend.planner.grounder import ROOT

TELEMETRY_FILE = ROOT / "data" / "telemetry.jsonl"
ULTRA_IN, ULTRA_OUT = 1.00, 3.00          # $ / 1M tokens — src/model_catalog.md (counterfactual baseline)
OSV_URL = "https://osv.dev/vulnerability/"

# palette (validated categorical slots 1-2 + neutral inks); status colours only with a text label
INK, INK_2, MUTED, RULE = colors.HexColor("#1f1f1d"), colors.HexColor("#55554f"), colors.HexColor("#8a8a82"), colors.HexColor("#e2e1dc")
SURFACE, PANEL = colors.white, colors.HexColor("#f6f5f2")
BEFORE_C, AFTER_C = colors.HexColor("#eb6834"), colors.HexColor("#2a78d6")
GOOD, WARN_C, BAD = colors.HexColor("#1a7f37"), colors.HexColor("#b26a00"), colors.HexColor("#c4314b")
DEL_BG, ADD_BG, HUNK_BG = colors.HexColor("#fde8e8"), colors.HexColor("#e6f4ea"), colors.HexColor("#eef3fb")
VERDICT_C = {"PASS": GOOD, "WARN": WARN_C, "FAIL": BAD, "ERROR": BAD}

_ss = getSampleStyleSheet()
S = {
    "title": ParagraphStyle("t", parent=_ss["Title"], fontName="Helvetica-Bold", fontSize=22, leading=26,
                            textColor=INK, alignment=0, spaceAfter=2),
    "sub": ParagraphStyle("s", fontName="Helvetica", fontSize=10, leading=13, textColor=INK_2),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=INK,
                         spaceBefore=10, spaceAfter=6),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=INK,
                         spaceBefore=8, spaceAfter=4),
    "body": ParagraphStyle("b", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK),
    "small": ParagraphStyle("sm", fontName="Helvetica", fontSize=7.5, leading=10, textColor=INK_2),
    "cell": ParagraphStyle("c", fontName="Helvetica", fontSize=8, leading=10.2, textColor=INK),
    "cellb": ParagraphStyle("cb", fontName="Helvetica-Bold", fontSize=8, leading=10.2, textColor=INK),
    "code": ParagraphStyle("code", fontName="Courier", fontSize=6.6, leading=8.2, textColor=INK),
    "kpi": ParagraphStyle("k", fontName="Helvetica-Bold", fontSize=17, leading=20, textColor=INK, alignment=TA_CENTER),
    "kpil": ParagraphStyle("kl", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=INK_2, alignment=TA_CENTER),
}

_UNSAFE = {"→": "->", "←": "<-", "≤": "<=", "≥": ">=", "✓": "ok", "✗": "x",
           "Δ": "delta ", "×": "x", "−": "-", "\t": "    "}


# --------------------------------------------------------------------------- #
# Text helpers (core PDF fonts are cp1252: keep text inside it)
# --------------------------------------------------------------------------- #
def safe(s: Any) -> str:
    s = "" if s is None else str(s)
    for k, v in _UNSAFE.items():
        s = s.replace(k, v)
    return s.encode("cp1252", "replace").decode("cp1252")


def esc(s: Any) -> str:
    return html.escape(safe(s), quote=False)


# Letter spacing (pt) for headings: ReportLab paragraphs can't track letters, so these use SpacedText.
HEADING_TRACKING = {"title": 1.2, "h1": 0.7, "h2": 0.45, "kpi": 0.6}


class SpacedText(Flowable):
    """One-style heading drawn with extra letter spacing (ReportLab Paragraphs have no char spacing).
    Wraps on words; honours the style's font, size, leading, colour, alignment and space before/after."""

    def __init__(self, text: str, style: ParagraphStyle, char_space: float):
        super().__init__()
        self.text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", text)).split())
        self.style, self.cs = style, char_space
        self.spaceBefore, self.spaceAfter = style.spaceBefore, style.spaceAfter
        self._lines: list[str] = []

    def _w(self, s: str) -> float:
        return stringWidth(s, self.style.fontName, self.style.fontSize) + self.cs * max(len(s) - 1, 0)

    def wrap(self, aw, ah):
        lines, cur = [], ""
        for word in self.text.split(" "):
            trial = f"{cur} {word}" if cur else word
            if cur and self._w(trial) > aw:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        lines.append(cur)
        self._lines, self.width = lines, aw
        self.height = self.style.leading * len(lines)
        return aw, self.height

    def draw(self):
        st, c = self.style, self.canv
        for i, line in enumerate(self._lines):
            lw = self._w(line)
            x = (self.width - lw) / 2 if st.alignment == 1 else (self.width - lw if st.alignment == 2 else 0)
            y = self.height - st.leading * (i + 1) + (st.leading - st.fontSize) * 0.5 + st.fontSize * 0.2
            t = c.beginText(x, y)
            t.setFont(st.fontName, st.fontSize)
            t.setCharSpace(self.cs)
            t.setFillColor(st.textColor)
            t.textOut(line)
            t.setCharSpace(0)
            c.drawText(t)


def P(text: str, style: str = "cell") -> Flowable:
    if style in HEADING_TRACKING and not re.search(r"<(?!br\b)[a-z]", text, re.I):
        return SpacedText(text, S[style], HEADING_TRACKING[style])
    return Paragraph(text, S[style])


def code_cell(line: str, width: int) -> Paragraph:
    """Monospace cell that keeps indentation and hard-wraps long lines."""
    line = safe(line.rstrip("\r\n"))
    chunks = textwrap.wrap(line, width, drop_whitespace=False, replace_whitespace=False) or [""]
    return Paragraph("<br/>".join(esc(c).replace(" ", "&nbsp;") for c in chunks), S["code"])


def money(x: float | None, digits: int = 4) -> str:
    return "-" if x is None else f"${x:,.{digits}f}"


def num(x: Any, digits: int = 2, suffix: str = "") -> str:
    if x is None:
        return "-"
    return f"{x:,.{digits}f}{suffix}" if isinstance(x, float) else f"{x:,}{suffix}"


def speed(x: float | None) -> str:
    if not x:
        return "-"
    return f"{x:.2f}x faster" if x >= 1 else f"{1 / x:.2f}x slower"


def link(url: str, text: str | None = None) -> str:
    return f'<link href="{html.escape(url)}" color="#2a78d6">{esc(text or url)}</link>'


def grid(rows: list[list[Any]], widths: list[float], *, header: bool = True, zebra: bool = True,
         extra: list[tuple] | None = None) -> Table:
    data = [[c if not isinstance(c, str) else P(esc(c), "cellb" if header and i == 0 else "cell")
             for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3.5),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5), ("LEFTPADDING", (0, 0), (-1, -1), 5),
          ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), PANEL), ("LINEBELOW", (0, 0), (-1, 0), 0.8, MUTED)]
    if zebra:
        st += [("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fbfbf9")) for i in range(2, len(rows), 2)]
    t.setStyle(TableStyle(st + (extra or [])))
    return t


def badge(text: str, color: colors.Color) -> str:
    return f'<font color="{color.hexval().replace("0x", "#")}"><b>{esc(text)}</b></font>'


# --------------------------------------------------------------------------- #
# Data assembly
# --------------------------------------------------------------------------- #
def collect_vulnerabilities(scan: dict, parity: dict, recheck: bool) -> dict:
    pkg = scan.get("package") or "pydantic"
    before_v = scan.get("current_version") or parity.get("envs", {}).get("before", {}).get("pydantic")
    after_v = parity.get("envs", {}).get("after", {}).get("pydantic") or scan.get("latest_version")
    before = scan.get("vulnerabilities")
    after, recheck_status = None, "Not re-checked"
    if recheck:
        from src.backend.extract.scanner import check_cve      # OSV.dev; returns (0, []) when offline
        try:
            if before is None and before_v:
                _, before = check_cve(pkg, before_v)
            if after_v:
                _, after = check_cve(pkg, after_v)
                recheck_status = f"Re-checked on OSV.dev for {pkg} {after_v}"
        except Exception as e:      # network / API shape change: report, don't fail
            recheck_status = f"Re-check failed: {type(e).__name__}"
    before = before or []
    after_ids = {v.get("id") for v in after or []}
    groups: dict[str, list[str]] = {}       # OSV lists one issue under several ids (GHSA + PYSEC aliases)
    for v in before:
        groups.setdefault((v.get("details") or v.get("id") or "").strip(), []).append(v.get("id"))
    items = [{"id": ids[0], "aliases": ids[1:], "details": det,
              "status": ("Open" if set(ids) & after_ids else "Resolved") if after is not None else "Resolved (version bump)"}
             for det, ids in groups.items()]
    return {"package": pkg, "before_version": before_v, "after_version": after_v, "items": items,
            "resolved": sum(i["status"].startswith("Resolved") for i in items),
            "remaining_after": len(after or []) if after is not None else None, "recheck": recheck_status}


def _plain_md(t: str) -> str:
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t or "")          # [text](url) -> text
    t = re.sub(r"^[\s*+|-]+", "", t).replace("`", "").replace(" | ", " -> ").strip(" |")
    return t


def collect_docs(stage3: dict) -> dict:
    g = stage3.get("grounding") or {}
    docs: dict[str, dict] = defaultdict(lambda: {"symbols": set(), "steps": set(), "retrieved": False})
    for url in g.get("sources", []):
        docs[url]["retrieved"] = True
    symbols = []
    for sym, info in (g.get("symbols") or {}).items():
        urls = {info.get("doc_url")} | {e.get("url") for e in info.get("evidence", [])}
        for ex in (info.get("examples") or {}).values():
            urls |= {e.get("url") for e in ex}
        for u in filter(None, urls):
            docs[u]["symbols"].add(sym)
        symbols.append({"symbol": sym, "v2": info.get("v2") or "(removed)", "topic": info.get("topic"),
                        "grounded": info.get("grounded"), "doc_url": info.get("doc_url"),
                        "evidence": _plain_md((info.get("evidence") or [{}])[0].get("text", ""))})
    for st in stage3.get("refactor_plan", []):
        if str(st.get("source", "")).startswith("http"):
            docs[st["source"]]["steps"].add(st.get("step"))
    rows = sorted(({"url": u, "symbols": sorted(d["symbols"]), "steps": sorted(x for x in d["steps"] if x is not None),
                    "retrieved": d["retrieved"]} for u, d in docs.items() if u.startswith("http")),
                  key=lambda r: (-len(r["steps"]) - len(r["symbols"]), r["url"]))
    return {"docs": rows, "symbols": symbols, "queries": g.get("queries", []), "stats": g.get("stats", {})}


def _ultra_cost(e: dict) -> float:
    return e.get("prompt_tokens", 0) / 1e6 * ULTRA_IN + e.get("completion_tokens", 0) / 1e6 * ULTRA_OUT


def run_entries(pipeline: dict) -> list[dict]:
    out = []
    plan_t = ((pipeline.get("stage3") or {}).get("planner") or {}).get("telemetry")
    if plan_t and (plan_t.get("prompt_tokens") or plan_t.get("completion_tokens")):
        out.append({"stage": "plan", **plan_t})
    out += [{"stage": e.get("stage", "patch"), **e} for e in (pipeline.get("stage4") or {}).get("telemetry", [])]
    return out


def ledger(entries: list[dict]) -> dict:
    by_model: dict[str, dict] = {}
    for e in entries:
        m = by_model.setdefault(e.get("model", "?"), {"tier": e.get("tier"), "dispatches": 0, "cached": 0,
                                                     "prompt_tokens": 0, "completion_tokens": 0, "actual_usd": 0.0,
                                                     "ultra_usd": 0.0, "stages": set()})
        cached = bool(e.get("cached"))
        m["dispatches"] += 1
        m["cached"] += cached
        m["prompt_tokens"] += e.get("prompt_tokens", 0)
        m["completion_tokens"] += e.get("completion_tokens", 0)
        m["actual_usd"] += 0.0 if cached else e.get("actual_cost_usd", 0.0)
        m["ultra_usd"] += _ultra_cost(e)
        m["stages"].add(e.get("stage", "?"))
    live = [e for e in entries if not e.get("cached")]
    actual = sum(e.get("actual_cost_usd", 0.0) for e in live)
    naive = sum(_ultra_cost(e) for e in entries)            # every call live, every call on Ultra
    routing = sum(_ultra_cost(e) - e.get("actual_cost_usd", 0.0) for e in live)
    cache = sum(_ultra_cost(e) for e in entries if e.get("cached"))
    tier_rank = {"lightning": 0, "super": 1, "ultra": 2}
    for m in by_model.values():
        m["stages"] = ", ".join(sorted(m["stages"]))
        m["savings_usd"] = m["ultra_usd"] - m["actual_usd"]
    return {
        "models": dict(sorted(by_model.items(), key=lambda kv: tier_rank.get(kv[1]["tier"], 9))),
        "dispatches": len(entries), "cached": len(entries) - len(live),
        "prompt_tokens": sum(e.get("prompt_tokens", 0) for e in entries),
        "completion_tokens": sum(e.get("completion_tokens", 0) for e in entries),
        "first_pass": sum(bool(e.get("first_pass_success")) for e in entries),
        "actual_usd": actual, "ultra_only_usd": naive, "routing_savings_usd": routing,
        "cache_savings_usd": cache, "total_saved_usd": naive - actual,
        "saved_pct": (naive - actual) / naive * 100 if naive else 0.0,
    }


def all_time_entries() -> list[dict]:
    try:
        return [json.loads(l) for l in TELEMETRY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return []


# --------------------------------------------------------------------------- #
# Diffs
# --------------------------------------------------------------------------- #
def _read(p: Path) -> str:
    try:
        with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
            return f.read().replace("\r\n", "\n")
    except OSError:
        return ""


def file_diffs(ws: Path) -> list[dict]:
    out = []
    for rel in refactored_files(ws):
        before, after = _read(_backup(ws) / rel), _read(ws / rel)
        a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
        if a and not a[-1].endswith("\n"):
            a[-1] += "\n\\ No newline at end of file\n"
        if b and not b[-1].endswith("\n"):
            b[-1] += "\n\\ No newline at end of file\n"
        body = "".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=3))
        unified = f"diff --git a/{rel} b/{rel}\n{body}" if body else ""
        added = sum(1 for l in body.splitlines() if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in body.splitlines() if l.startswith("-") and not l.startswith("---"))
        out.append({"file": rel, "before": before, "after": after, "unified": unified,
                    "added": added, "removed": removed})
    return out


def side_by_side_rows(before: str, after: str, context: int = 3) -> list[tuple]:
    """(kind, left_no, left, right_no, right); kind in equal/delete/insert/replace/skip."""
    a, b = before.splitlines(), after.splitlines()
    rows: list[tuple] = []
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            n = i2 - i1
            head = context if idx > 0 else 0
            tail = context if idx < len(ops) - 1 else 0
            if n > head + tail + 1:
                rows += [("equal", i1 + k + 1, a[i1 + k], j1 + k + 1, b[j1 + k]) for k in range(head)]
                rows.append(("skip", None, f"... {n - head - tail} unchanged lines ...", None, ""))
                rows += [("equal", i2 - tail + k + 1, a[i2 - tail + k], j2 - tail + k + 1, b[j2 - tail + k])
                         for k in range(tail)]
            else:
                rows += [("equal", i1 + k + 1, a[i1 + k], j1 + k + 1, b[j1 + k]) for k in range(n)]
        else:
            for k in range(max(i2 - i1, j2 - j1)):
                l = (i1 + k + 1, a[i1 + k]) if i1 + k < i2 else (None, "")
                r = (j1 + k + 1, b[j1 + k]) if j1 + k < j2 else (None, "")
                rows.append((tag, l[0], l[1], r[0], r[1]))
    return rows


def export_diffs(diffs: list[dict], out: Path) -> dict:
    ddir = out / "diffs"
    ddir.mkdir(parents=True, exist_ok=True)
    files = []
    hd = difflib.HtmlDiff(tabsize=4, wrapcolumn=100)
    for d in diffs:
        flat = d["file"].replace("/", "__")
        (ddir / f"{flat}.patch").write_text(d["unified"], encoding="utf-8", newline="\n")
        page = hd.make_file(d["before"].splitlines(), d["after"].splitlines(), f"a/{d['file']} (before)",
                            f"b/{d['file']} (after)", context=True, numlines=3)
        page = page.replace("<head>", '<head>\n<meta charset="utf-8">', 1)
        (ddir / f"{flat}.html").write_text(page, encoding="utf-8")
        files.append({"file": d["file"], "patch": str(ddir / f"{flat}.patch"), "html": str(ddir / f"{flat}.html")})
    combined = out / "reforge.patch"
    combined.write_text("".join(d["unified"] for d in diffs), encoding="utf-8", newline="\n")
    return {"combined": str(combined), "files": files}


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def _short(label: str, n: int = 28) -> str:
    return label if len(label) <= n else label[:n - 3] + "..."


def paired_bars(labels: list[str], before: list[float], after: list[float], width: float, unit: str,
                names: tuple[str, str] = ("Before", "After")) -> Drawing:
    row_h = 26
    h = max(90, row_h * len(labels) + 46)
    d = Drawing(width, h)
    c = HorizontalBarChart()
    c.x, c.y, c.width, c.height = 150, 26, width - 175, h - 46
    c.data = [list(reversed(before)), list(reversed(after))]
    c.categoryAxis.categoryNames = [safe(l) for l in reversed(labels)]
    c.categoryAxis.labels.fontName, c.categoryAxis.labels.fontSize = "Helvetica", 7.5
    c.categoryAxis.labels.fillColor = INK_2
    c.categoryAxis.labels.boxAnchor, c.categoryAxis.labels.dx = "e", -4
    c.categoryAxis.strokeColor = RULE
    c.categoryAxis.tickLeft = 0
    c.valueAxis.valueMin = 0
    c.valueAxis.labels.fontName, c.valueAxis.labels.fontSize = "Helvetica", 7
    c.valueAxis.labels.fillColor = MUTED
    c.valueAxis.strokeColor = RULE
    c.valueAxis.visibleGrid, c.valueAxis.gridStrokeColor, c.valueAxis.gridStrokeWidth = 1, RULE, 0.4
    c.bars[0].fillColor, c.bars[1].fillColor = BEFORE_C, AFTER_C
    c.bars.strokeColor, c.bars.strokeWidth = SURFACE, 1.5          # 2px surface gap between paired bars
    c.barSpacing, c.groupSpacing = 1, 7
    d.add(c)
    lg = Legend()
    lg.x, lg.y = 150, h - 6
    lg.alignment, lg.columnMaximum, lg.deltax = "right", 1, 90
    lg.fontName, lg.fontSize, lg.fillColor = "Helvetica", 7.5, INK_2
    lg.boxAnchor, lg.dxTextSpace, lg.dx, lg.dy = "nw", 4, 8, 8
    lg.strokeColor = None
    lg.colorNamePairs = [(BEFORE_C, names[0]), (AFTER_C, names[1])]
    d.add(lg)
    d.add(String(width - 22, 8, unit, fontName="Helvetica", fontSize=7, fillColor=MUTED, textAnchor="end"))
    return d


# --------------------------------------------------------------------------- #
# PDF sections
# --------------------------------------------------------------------------- #
PW, PH = A4
LW, LH = landscape(A4)
M = 16 * mm
CW = PW - 2 * M        # portrait content width
LCW = LW - 2 * M       # landscape content width


def kpi_tiles(tiles: list[tuple[str, str, str]]) -> Table:
    cells = [[P(esc(v), "kpi"), P(esc(l) + (f"<br/><font color='#8a8a82'>{esc(s)}</font>" if s else ""), "kpil")]
             for v, l, s in tiles]
    row = [Table([[x] for x in c], colWidths=[CW / 3 - 8], style=TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                                            ("TOPPADDING", (0, 0), (-1, -1), 2)])) for c in cells]
    rows = [row[i:i + 3] for i in range(0, len(row), 3)]
    t = Table(rows, colWidths=[CW / 3] * 3)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PANEL), ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                           ("INNERGRID", (0, 0), (-1, -1), 3, SURFACE), ("TOPPADDING", (0, 0), (-1, -1), 9),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    return t


def section_summary(ctx: dict) -> list:
    p, v, cost, perf, tests = ctx["parity"], ctx["vulns"], ctx["cost"], ctx["perf"], ctx["parity"].get("tests", {})
    verdict = p.get("verdict", "N/A")
    tb, ta = tests.get("before", {}), tests.get("after", {})
    out = p.get("outputs", {})
    st4 = ctx["pipeline"].get("stage4") or {}
    story = [
        P("Reforge Audit Report", "title"),
        P(f"{esc(ctx['repo'])} &nbsp;&middot;&nbsp; Pydantic {esc(v['before_version'] or '1.x')} -&gt; "
          f"{esc(v['after_version'] or '2.x')} &nbsp;&middot;&nbsp; {esc(ctx['generated'])}", "sub"),
        Spacer(1, 10),
    ]
    vc = VERDICT_C.get(verdict, MUTED)
    vt = Table([[P(f"<font color='white'><b>PARITY {esc(verdict)}</b></font>", "cell"),
                 P(esc("; ".join(p.get("reasons", [])) or "Original and refactored code behave the same: tests, "
                       "return values, model schemas and entrypoint output match."), "cell")]],
               colWidths=[32 * mm, CW - 32 * mm])
    vt.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), vc), ("BOX", (0, 0), (-1, -1), 0.6, vc),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    story += [vt, Spacer(1, 10)]

    hot = perf.get("delta", {}).get("hot_path") or {}
    tiles = [
        (f"{v['resolved']}/{len(v['items'])}" if v["items"] else "0", "Vulnerabilities resolved",
         f"{v['remaining_after']} open in {v['after_version']}" if v["remaining_after"] is not None else v["recheck"]),
        (f"{num(tb.get('pass_rate'), 0)}% -> {num(ta.get('pass_rate'), 0)}%", "Test pass rate",
         f"{ta.get('passed', 0)}/{ta.get('collected', 0)} tests after refactor"),
        (f"{num(out.get('parity_pct'), 0)}%" if out.get("parity_pct") is not None else "n/a", "Output parity",
         f"{out.get('identical', 0)} identical, {out.get('equivalent', 0)} equivalent of {out.get('compared', 0)}"),
        (speed(hot.get("speedup_x")).replace(" faster", "") if hot else "-", "Hot-path speedup",
         safe(hot.get("test", "").split("::")[-1])[:40] if hot else ""),
        (money(cost["actual_usd"], 4), "AI spend this run",
         f"{cost['dispatches']} dispatches, {cost['prompt_tokens'] + cost['completion_tokens']:,} tokens"),
        (money(cost["total_saved_usd"], 4), "Saved vs Ultra-only",
         f"{cost['saved_pct']:.0f}% overall" + (f", {ctx['patch_saved_pct']:.0f}% on patching"
                                               if ctx.get("patch_saved_pct") is not None else "")),
    ]
    story += [kpi_tiles(tiles), Spacer(1, 12), P("Summary", "h1")]

    plan = (ctx["pipeline"].get("stage3") or {}).get("refactor_plan", [])
    bullets = [
        f"<b>Migration.</b> {len(ctx['diffs'])} file(s) refactored "
        f"(+{sum(d['added'] for d in ctx['diffs'])} / -{sum(d['removed'] for d in ctx['diffs'])} lines) "
        f"from {'an' if str(len(plan)).startswith('8') or len(plan) in (11, 18) else 'a'} {len(plan)}-step plan; Stage 4 status: <b>{esc(st4.get('status', 'n/a'))}</b>"
        f"{', with escalation' if st4.get('escalated') else ''}.",
        f"<b>Security.</b> {len(v['items'])} known advisory(ies) affected {esc(v['package'])} {esc(v['before_version'])}; "
        f"{v['resolved']} resolved by moving to {esc(v['after_version'])} ({esc(v['recheck'].lower())}).",
        f"<b>Behaviour.</b> {tb.get('passed', 0)}/{tb.get('collected', 0)} tests passed on the original (v1), "
        f"{ta.get('passed', 0)}/{ta.get('collected', 0)} on the refactor (v2); "
        f"{len(tests.get('regressions', []))} regression(s). {out.get('compared', 0)} recorded outputs compared, "
        + (f"{out.get('nondeterministic', 0)} nondeterministic (judged on structure)."
           if p.get("settings", {}).get("capture_runs", 1) > 1 else "single capture run."),
        f"<b>Performance.</b> Test suite {speed(perf.get('delta', {}).get('test_time_speedup_x'))}; "
        f"peak memory {num(perf.get('before', {}).get('peak_rss_mb'), 1)} -&gt; {num(perf.get('after', {}).get('peak_rss_mb'), 1)} MB.",
        f"<b>Cost.</b> {money(cost['actual_usd'])} spent across {len(cost['models'])} model(s); the same tokens on "
        f"Nemotron Ultra alone would cost {money(cost['ultra_only_usd'])}.",
        f"<b>Grounding.</b> {len(ctx['docs']['docs'])} official documentation page(s) cited for "
        f"{len(ctx['docs']['symbols'])} migrated symbol(s).",
    ]
    story += [P("&bull;&nbsp; " + b, "body") for b in bullets]
    return story


def section_security(ctx: dict) -> list:
    v = ctx["vulns"]
    story = [P("1. Security vulnerabilities resolved", "h1"),
             P(f"Advisories from OSV.dev for <b>{esc(v['package'])} {esc(v['before_version'])}</b> (original pin), "
               f"checked against <b>{esc(v['after_version'])}</b> (post-refactor pin). {esc(v['recheck'])}. "
               f"Advisory ids that OSV lists for the same issue are grouped in one row.", "body"),
             Spacer(1, 6)]
    if not v["items"]:
        return story + [P("No known advisories affected the original version.", "body")]
    rows = [["Advisory", "Description", f"In {v['before_version']}", f"In {v['after_version']}"]]
    extra = []
    for i, it in enumerate(v["items"], 1):
        ok = it["status"].startswith("Resolved")
        ids = "<br/>".join(link(OSV_URL + i, i) for i in [it["id"], *it.get("aliases", [])])
        rows.append([P(ids + ("<br/><font color='#8a8a82'>(aliases)</font>" if it.get("aliases") else "")), P(esc(textwrap.shorten(it["details"] or "-", 420))),
                     P(badge("Affected", BAD)), P(badge(it["status"], GOOD if ok else BAD))])
    story.append(grid(rows, [34 * mm, CW - 34 * mm - 2 * 26 * mm, 26 * mm, 26 * mm], extra=extra))
    return story


def section_docs(ctx: dict) -> list:
    d = ctx["docs"]
    story = [P("2. Official documentation used", "h1"),
             P("Every change was grounded in the Pydantic v2 docs before patching. Pages below were retrieved live "
               "(Tavily, restricted to docs.pydantic.dev) or come from Reforge's curated migration KB.", "body"),
             Spacer(1, 6)]
    if d["docs"]:
        rows = [["Source", "Symbols grounded", "Plan steps", "Origin"]]
        for r in d["docs"][:25]:
            rows.append([P(link(r["url"])), P(esc(", ".join(r["symbols"]) or "-")),
                         P(esc(", ".join(map(str, r["steps"])) or "-")), P("live" if r["retrieved"] else "KB")])
        story.append(grid(rows, [CW * 0.47, CW * 0.31, CW * 0.12, CW * 0.10]))
    if d["symbols"]:
        story += [P("Symbol mapping (v1 -> v2)", "h2")]
        rows = [["v1 symbol", "v2 replacement", "Topic", "Evidence"]]
        for s in d["symbols"]:
            ev = textwrap.shorten(s["evidence"], 160) if s["evidence"] else ("docs: " + (s["doc_url"] or "-"))
            rows.append([P(f"<font name='Courier'>{esc(s['symbol'])}</font>"),
                         P(f"<font name='Courier'>{esc(s['v2'])}</font>"), P(esc(s["topic"] or "-")),
                         P(esc(ev) + ("" if s["grounded"] else " <font color='#8a8a82'>(KB)</font>"))])
        story.append(grid(rows, [CW * 0.22, CW * 0.24, CW * 0.14, CW * 0.40]))
    if not d["docs"] and not d["symbols"]:
        story.append(P("No grounding data in the pipeline result.", "body"))
    return story


def section_performance(ctx: dict) -> list:
    p, perf = ctx["parity"], ctx["perf"]
    tests, out, sch = p.get("tests", {}), p.get("outputs", {}), p.get("schemas", {})
    tb, ta = tests.get("before", {}), tests.get("after", {})
    b, a, dl = perf.get("before", {}), perf.get("after", {}), perf.get("delta", {})
    envs = p.get("envs", {})
    hot = dl.get("hot_path") or {}
    s2 = tests.get("stage2_unpatched_v2") or {}
    st2 = ctx["pipeline"].get("stage2") or {}
    if st2:
        n_total = s2.get("collected") or (ctx["pipeline"].get("baseline") or {}).get("collected") or tb.get("collected", 0)
        s2_note = (f" For reference, the original code under v2 without the refactor passed {s2.get('passed', 0)}/{n_total} "
                   f"tests (Stage 2: {esc(st2.get('test_status', 'n/a'))}).")
    else:
        s2_note = ""

    def delta_pts(x, y):
        return f"{y - x:+.1f} pts" if x is not None and y is not None else "-"

    def delta_mb(x):
        return f"{x:+.1f} MB" if x is not None else "-"

    rows = [
        ["Metric", f"Before (v1 {envs.get('before', {}).get('pydantic', '')}, original)",
         f"After (v2 {envs.get('after', {}).get('pydantic', '')}, refactored)", "Change"],
        ["Test pass rate", f"{num(tb.get('pass_rate'), 1)}%", f"{num(ta.get('pass_rate'), 1)}%",
         delta_pts(tb.get("pass_rate"), ta.get("pass_rate"))],
        ["Tests passed", f"{tb.get('passed', 0)}/{tb.get('collected', 0)}", f"{ta.get('passed', 0)}/{ta.get('collected', 0)}",
         f"{len(tests.get('regressions', []))} regressed, {len(tests.get('fixed', []))} fixed"],
        ["Output parity (return values + stdout)", "reference",
         f"{num(out.get('parity_pct'), 1)}% match" if out.get("parity_pct") is not None else "n/a",
         f"{out.get('similar', 0)} similar, {out.get('divergent', 0)} divergent"],
        ["Model schema parity", f"{sch.get('models', 0)} models",
         f"{sch.get('identical', 0)} identical", f"{sch.get('constraint_drift', 0)} drift, {sch.get('divergent', 0)} changed"],
        ["Peak memory (test run)", num(b.get("peak_rss_mb"), 1, " MB"), num(a.get("peak_rss_mb"), 1, " MB"),
         delta_mb(dl.get("peak_rss_delta_mb"))],
        ["Import footprint", num(b.get("import_rss_mb"), 1, " MB"), num(a.get("import_rss_mb"), 1, " MB"),
         delta_mb(dl.get("import_rss_delta_mb"))],
        ["Test suite time (median)", num(b.get("test_time_s"), 3, " s"), num(a.get("test_time_s"), 3, " s"),
         speed(dl.get("test_time_speedup_x"))],
        ["Process wall time (median)", num(b.get("wall_s"), 3, " s"), num(a.get("wall_s"), 3, " s"),
         speed(dl.get("wall_speedup_x"))],
    ]
    if hot:
        rows.append([f"Hot path: {safe(hot['test'].split('::')[-1])}", num(hot["before_s"], 4, " s"),
                     num(hot["after_s"], 4, " s"), speed(hot["speedup_x"])])
    hl = [("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), colors.HexColor("#eef3fb"))] if hot else []
    story = [P("3. Performance matrix", "h1"),
             P(f"Same tests, same machine, original code on Pydantic v1 vs refactored code on Pydantic v2 "
               f"(median of {b.get('runs', 0)} timed runs each, network blocked, secrets blanked)."
               + s2_note, "body"),
             Spacer(1, 6), grid(rows, [CW * 0.34, CW * 0.2, CW * 0.2, CW * 0.26], extra=hl)]

    per = dl.get("per_test", [])[:8]
    if per:
        story += [P("Per-test latency", "h2"),
                  paired_bars([_short(t["test"].split("::")[-1].removeprefix("test_")) for t in per], [t["before_s"] for t in per],
                              [t["after_s"] for t in per], CW, "seconds", ("Before (v1)", "After (v2)"))]

    # parity detail
    story += [CondPageBreak(60 * mm), P("Output parity detail", "h2"),
              P(f"Return values of the repo's top-level functions and each test's stdout were recorded on both sides "
                f"({p.get('settings', {}).get('capture_runs', 1)} run(s) each). <b>identical</b> = same value; "
                f"<b>equivalent</b> = same structure and types, different values (expected for LLM text, hashes, "
                f"timestamps); <b>similar</b> = structure overlaps >= {int(p.get('settings', {}).get('similar_at', 0.8) * 100)}%; "
                f"<b>divergent</b> = structure changed.", "body"), Spacer(1, 4)]
    fns = out.get("functions", {})
    if fns:
        rows = [["Function", "Calls", "Identical", "Equivalent", "Similar", "Divergent"]]
        for fn, s in sorted(fns.items()):
            rows.append([P(f"<font name='Courier'>{esc(fn)}</font>"), str(s["calls"]), str(s["identical"]),
                         str(s["equivalent"]), str(s["similar"]), str(s["divergent"])])
        story.append(grid(rows, [CW * 0.45] + [CW * 0.11] * 5))
    items = [i for i in out.get("items", []) if i["level"] != "equivalent"][:12] or out.get("items", [])[:6]
    if items:
        rows = [["Where", "Level", "Before", "After"]]
        for i in items:
            where = f"{i.get('fn', 'stdout')} #{i.get('call', '')}<br/><font color='#8a8a82'>{esc(i['test'].split('::')[-1])}</font>"
            lvl = i["level"] + (" (nondet.)" if i.get("nondeterministic") else "")
            rows.append([P(where.replace("::", " :: ")), P(esc(lvl)), code_cell(i.get("before", ""), 48),
                         code_cell(i.get("after", ""), 48)])
        story += [Spacer(1, 6), grid(rows, [CW * 0.22, CW * 0.12, CW * 0.33, CW * 0.33])]
    changed = [m for m in sch.get("items", []) if m["level"] != "identical"]
    if changed:
        rows = [["Model", "Level", "Changes"]]
        for m in changed[:15]:
            rows.append([P(f"<font name='Courier'>{esc(m['model'])}</font>"), P(esc(m["level"])),
                         P("<br/>".join(esc(c) for c in m["changes"][:6]))])
        story += [Spacer(1, 6), grid(rows, [CW * 0.28, CW * 0.14, CW * 0.58])]
    for e in p.get("entrypoints", []):
        story += [Spacer(1, 4), P(f"Entrypoint <font name='Courier'>{esc(e['script'])}</font>: <b>{esc(e['level'])}</b>"
                                  + (f" ({esc(e.get('error'))})" if e.get("error") else ""), "body")]
    return story


def section_cost(ctx: dict) -> list:
    c = ctx["cost"]
    story = [P("4. AI cost ledger", "h1"),
             P("Every Nemotron dispatch of this run (Stage 3 planning + Stage 4 patching/healing), priced with "
               "src/model_catalog.md. The counterfactual prices the same tokens on Nemotron 3 Ultra, the model a "
               "single-model agent would use; cached replies cost $0.", "body"), Spacer(1, 6)]
    rows = [["Model", "Tier", "Stages", "Calls (cached)", "Input tok", "Output tok", "Spend", "Ultra-only", "Saved"]]
    for m, s in c["models"].items():
        rows.append([P(f"<font name='Courier' size='6.5'>{esc(m.split('/')[-1])}</font>"), s["tier"] or "-", s["stages"],
                     f"{s['dispatches']} ({s['cached']})", f"{s['prompt_tokens']:,}", f"{s['completion_tokens']:,}",
                     money(s["actual_usd"]), money(s["ultra_usd"]), money(s["savings_usd"])])
    rows.append([P("<b>Total</b>"), "", "", f"{c['dispatches']} ({c['cached']})", f"{c['prompt_tokens']:,}",
                 f"{c['completion_tokens']:,}", P(f"<b>{money(c['actual_usd'])}</b>"), money(c["ultra_only_usd"]),
                 P(f"<b>{money(c['total_saved_usd'])}</b>")])
    w = [CW * x for x in (0.25, 0.08, 0.09, 0.09, 0.1, 0.1, 0.1, 0.1, 0.09)]
    story.append(grid(rows, w, extra=[("LINEABOVE", (0, -1), (-1, -1), 0.8, MUTED),
                                      ("BACKGROUND", (0, -1), (-1, -1), PANEL)]))
    story += [Spacer(1, 8), grid([
        ["Ledger line", "Amount"],
        ["Actual spend (live calls)", money(c["actual_usd"], 6)],
        ["Ultra-only counterfactual (all tokens, no cache)", money(c["ultra_only_usd"], 6)],
        ["Saved by routing (Lightning / Super first, Ultra on escalation)", money(c["routing_savings_usd"], 6)],
        ["Saved by reply cache", money(c["cache_savings_usd"], 6)],
        [P("<b>Total money saved</b>"), P(f"<b>{money(c['total_saved_usd'], 6)} ({c['saved_pct']:.1f}%)</b>")],
        ["First-pass success", f"{c['first_pass']}/{c['dispatches']} dispatches"],
    ], [CW * 0.7, CW * 0.3])]
    if c["models"]:
        names = list(c["models"])
        story += [P("Spend per model vs Ultra-only", "h2"),
                  paired_bars([_short(n.split("/")[-1]) for n in names], [c["models"][n]["actual_usd"] for n in names],
                              [c["models"][n]["ultra_usd"] for n in names], CW, "USD", ("Actual", "Ultra-only"))]
    story += routing_log(ctx)
    at = ctx["cost_all_time"]
    if at["dispatches"]:
        story += [P("All-time (data/telemetry.jsonl)", "h2"),
                  P(f"{at['dispatches']} live dispatches, {at['prompt_tokens'] + at['completion_tokens']:,} tokens: "
                    f"spent {money(at['actual_usd'])} vs {money(at['ultra_only_usd'])} Ultra-only, "
                    f"saved {money(at['total_saved_usd'])} ({at['saved_pct']:.1f}%).", "body")]
    return story


def routing_log(ctx: dict) -> list:
    """Stage-4 model escalation per plan step + heal rounds: where the money went and why."""
    st4 = ctx["pipeline"].get("stage4") or {}
    steps = st4.get("steps") or []
    if not steps:
        return []
    plan = {s.get("step"): s for s in (ctx["pipeline"].get("stage3") or {}).get("refactor_plan", [])}
    rows = [["Step", "File / action", "Route", "Result"]]
    extra = []
    for i, s in enumerate(steps, 1):
        route = " -> ".join(a.get("tier") or "?" for a in s.get("attempts", [])) or (s.get("tier") or "-")
        status = str(s.get("status") or "-")
        failed = status.startswith("FAILED")
        noop = "identical" in status
        if failed and noop:
            status = "No change needed (already applied by an earlier step)"
        action = (plan.get(s.get("step")) or {}).get("action") or s.get("action") or ""
        rows.append([str(s.get("step", i)), P(f"<font name='Courier'>{esc(s.get('file'))}</font><br/>"
                                              f"{esc(textwrap.shorten(action, 110))}"),
                     P(esc(route)), P(esc(textwrap.shorten(status, 90)))])
        if failed and not noop:
            extra.append(("TEXTCOLOR", (3, i), (3, i), BAD))
    v = st4.get("verification") or {}
    for r in v.get("heal_rounds", []):
        rows.append(["heal", P(f"Test-driven fix after first verify ({esc(v.get('first_run', '?'))})"),
                     P(esc(r.get("tier"))), P(esc(textwrap.shorten(
                         ("rolled back: " if r.get("rolled_back") else "") + str(r.get("status")), 90)))])
    return [CondPageBreak(50 * mm), P("Routing log (Stage 4)", "h2"),
            P(f"{esc(st4.get('steps_applied', '?'))} plan steps applied; final verification "
              f"{esc(v.get('status', 'n/a'))}. Each file gets one Lightning call for all its steps; a step it "
              f"misses falls back to the plan's own edit ($0), then Super, then Ultra.", "body"), Spacer(1, 4),
            grid(rows, [CW * 0.07, CW * 0.5, CW * 0.18, CW * 0.25], extra=extra)]


def section_diffs(ctx: dict) -> list:
    story: list = [NextPageTemplate("landscape"), PageBreak(), P("5. Code diffs", "h1"),
                   P(f"{len(ctx['diffs'])} refactored file(s). Original (left / -) vs refactored (right / +). "
                     f"Full patches: reforge.patch and diffs/ next to this PDF.", "body")]
    if not ctx["diffs"]:
        return story + [P("No file changes found (Stage-4 backup matches the workspace).", "body")]
    colw = [10 * mm, LCW / 2 - 10 * mm, 10 * mm, LCW / 2 - 10 * mm]
    width_chars = int((LCW / 2 - 10 * mm - 10) / (6.6 * 0.6))
    for d in ctx["diffs"]:
        story += [P(f"<font name='Courier'>{esc(d['file'])}</font> &nbsp; "
                    f"<font color='#1a7f37'>+{d['added']}</font> <font color='#c4314b'>-{d['removed']}</font>", "h2"),
                  P("Side by side", "small")]
        rows, style = [], [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 0.6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0.6), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("LINEAFTER", (1, 0), (1, -1), 0.5, RULE),
                           ("TEXTCOLOR", (0, 0), (-1, -1), MUTED)]
        for i, (kind, ln, left, rn, right) in enumerate(side_by_side_rows(d["before"], d["after"])):
            if kind == "skip":
                rows.append([P(""), P(f"<font color='#8a8a82'>{esc(left)}</font>", "code"), P(""), P("")])
                style.append(("BACKGROUND", (0, i), (-1, i), HUNK_BG))
                continue
            rows.append([P(f"<font color='#8a8a82'>{ln or ''}</font>", "code"), code_cell(left, width_chars),
                         P(f"<font color='#8a8a82'>{rn or ''}</font>", "code"), code_cell(right, width_chars)])
            if kind in ("delete", "replace") and ln:
                style.append(("BACKGROUND", (0, i), (1, i), DEL_BG))
            if kind in ("insert", "replace") and rn:
                style.append(("BACKGROUND", (2, i), (3, i), ADD_BG))
        t = Table(rows, colWidths=colw)
        t.setStyle(TableStyle(style + [("BOX", (0, 0), (-1, -1), 0.5, RULE)]))
        story += [t, Spacer(1, 8), CondPageBreak(45 * mm), P("Unified (git)", "small")]
        lines = d["unified"].splitlines()
        rows, style = [], [("TOPPADDING", (0, 0), (-1, -1), 0.4), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.4),
                           ("LEFTPADDING", (0, 0), (-1, -1), 4), ("BOX", (0, 0), (-1, -1), 0.5, RULE)]
        for i, l in enumerate(lines):
            rows.append([code_cell(l, int((LCW - 8) / (6.6 * 0.6)))])
            if l.startswith(("diff --git", "---", "+++")):
                style.append(("BACKGROUND", (0, i), (0, i), PANEL))
            elif l.startswith("@@"):
                style.append(("BACKGROUND", (0, i), (0, i), HUNK_BG))
            elif l.startswith("+"):
                style.append(("BACKGROUND", (0, i), (0, i), ADD_BG))
            elif l.startswith("-"):
                style.append(("BACKGROUND", (0, i), (0, i), DEL_BG))
        if rows:
            t = Table(rows, colWidths=[LCW])
            t.setStyle(TableStyle(style))
            story.append(t)
        story.append(CondPageBreak(70 * mm))
    return story


def _page_deco(title: str):
    def draw(canvas, doc):
        canvas.saveState()
        w, h = canvas._pagesize
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(M, 9 * mm, safe(title))
        canvas.drawRightString(w - M, 9 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(RULE)
        canvas.line(M, 12 * mm, w - M, 12 * mm)
        canvas.restoreState()
    return draw


def render_pdf(ctx: dict, path: Path) -> None:
    title = f"Reforge audit - {ctx['repo']} - {ctx['generated']}"
    doc = BaseDocTemplate(str(path), pagesize=A4, title="Reforge Audit Report", author="Reforge",
                          subject=f"Pydantic v1 -> v2 migration audit: {ctx['repo']}",
                          leftMargin=M, rightMargin=M, topMargin=M, bottomMargin=M + 4 * mm)
    deco = _page_deco(title)
    doc.addPageTemplates([
        PageTemplate("portrait", [Frame(M, M + 4 * mm, CW, PH - 2 * M - 4 * mm, id="p")], onPage=deco, pagesize=A4),
        PageTemplate("landscape", [Frame(M, M + 4 * mm, LCW, LH - 2 * M - 4 * mm, id="l")], onPage=deco,
                     pagesize=landscape(A4)),
    ])
    story = (section_summary(ctx) + [PageBreak()] + section_security(ctx) + [Spacer(1, 6)] + section_docs(ctx)
             + [CondPageBreak(110 * mm)] + section_performance(ctx) + [PageBreak()] + section_cost(ctx)
             + section_diffs(ctx))
    doc.build(story)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def build_report(ws_path: str | Path, pipeline: dict | str | Path | None = None, parity: dict | str | Path | None = None,
                 *, run_parity: bool = True, recheck_cves: bool = True, out_dir: str | Path | None = None,
                 **verifier_kwargs: Any) -> dict[str, Any]:
    ws = Path(ws_path).resolve()
    out = Path(out_dir) if out_dir else AUDIT_DIR / ws.name
    out.mkdir(parents=True, exist_ok=True)
    if isinstance(pipeline, (str, Path)):
        pipeline = json.loads(Path(pipeline).read_text(encoding="utf-8"))
    pipeline = pipeline or {}
    if isinstance(parity, (str, Path)):
        parity = json.loads(Path(parity).read_text(encoding="utf-8"))
    if parity is None and (out / "parity.json").is_file() and not run_parity:
        parity = json.loads((out / "parity.json").read_text(encoding="utf-8"))
        parity.setdefault("path", str(out / "parity.json"))
    if parity is None and run_parity:
        parity = run_verifier(ws, baseline=pipeline.get("baseline"), scan=pipeline.get("scan"),
                              stage2=pipeline.get("stage2"), out_dir=out, **verifier_kwargs)
    parity = parity or {"verdict": "N/A", "reasons": ["parity not run"]}

    entries = run_entries(pipeline)
    diffs = file_diffs(ws)
    ctx = {
        "repo": ws.name, "pipeline": pipeline, "parity": parity, "diffs": diffs,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "vulns": collect_vulnerabilities(pipeline.get("scan") or {}, parity, recheck_cves),
        "docs": collect_docs(pipeline.get("stage3") or {}),
        "perf": parity.get("performance", {}),
        "cost": ledger(entries),
        "patch_saved_pct": (lambda c: c["saved_pct"] if c["dispatches"] else None)(
            ledger([e for e in entries if e.get("stage") != "plan"])),
        "cost_all_time": ledger([e for e in all_time_entries() if not e.get("cached")]),
    }

    exported = export_diffs(diffs, out)
    with (out / "cost_ledger.csv").open("w", newline="", encoding="utf-8") as f:
        cols = ["timestamp", "stage", "step", "tier", "model", "prompt_tokens", "completion_tokens", "latency_ms",
                "actual_cost_usd", "counterfactual_ultra_cost_usd", "savings_usd", "cached", "first_pass_success"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(entries)
    pdf = out / "report.pdf"
    render_pdf(ctx, pdf)
    return {
        "pdf": str(pdf), "patch": exported["combined"], "diffs": exported["files"],
        "cost_ledger_csv": str(out / "cost_ledger.csv"), "parity_json": parity.get("path"),
        "verdict": parity.get("verdict"), "reasons": parity.get("reasons", []),
        "summary": {
            "vulnerabilities_resolved": ctx["vulns"]["resolved"], "vulnerabilities_total": len(ctx["vulns"]["items"]),
            "pass_rate_before": parity.get("tests", {}).get("before", {}).get("pass_rate"),
            "pass_rate_after": parity.get("tests", {}).get("after", {}).get("pass_rate"),
            "output_parity_pct": parity.get("outputs", {}).get("parity_pct"),
            "test_time_speedup_x": ctx["perf"].get("delta", {}).get("test_time_speedup_x"),
            "ai_spend_usd": round(ctx["cost"]["actual_usd"], 6),
            "saved_usd": round(ctx["cost"]["total_saved_usd"], 6),
            "files_refactored": len(diffs),
        },
    }


def run_stage5(ws_path: str | Path, pipeline: dict, **kwargs: Any) -> dict[str, Any]:
    """Endpoint entry point: parity verification + audit report."""
    return build_report(ws_path, pipeline, None, run_parity=True, **kwargs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 5: compile the audit report PDF")
    ap.add_argument("ws_path")
    ap.add_argument("pipeline_json", nargs="?", help="endpoint response (scan/stage2/stage3/stage4)")
    ap.add_argument("--parity", help="existing parity.json (skips the verifier)")
    ap.add_argument("--reuse-parity", action="store_true", help="use data/audit/<ws>/parity.json if present")
    ap.add_argument("--no-cve-recheck", action="store_true", help="don't query OSV.dev for the new version")
    ap.add_argument("--out", help="output directory (default data/audit/<ws>)")
    a = ap.parse_args(argv)
    res = build_report(a.ws_path, a.pipeline_json, a.parity, run_parity=not a.reuse_parity,
                       recheck_cves=not a.no_cve_recheck, out_dir=a.out)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":   # from the Reforge root: python -m src.backend.audit.report workspace/<repo> out.json
    raise SystemExit(main())
