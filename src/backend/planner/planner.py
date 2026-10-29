"""
planner/planner.py — Reforge Stage 3 (architect half): one Nemotron 3 Ultra call that turns
Stage-2 breakage + Stage-3 grounding into a strict, ordered refactor checklist.

Context sent in the single call:
  * affected files in Stage-2 order (schema -> callers), each with line-numbered source,
    static findings and the v2 crash messages
  * the repo's test files, READ-ONLY, so the plan preserves the behaviour they assert
  * per-symbol grounding (v2 replacement, doc evidence, before/after examples, caveats)

The JSON reply is validated, not trusted:
  * steps on unknown files or on tests are dropped; line numbers are clamped to the file
  * every Stage-2 finding must be covered; uncovered ones get a deterministic step from
    the grounding KB (origin="coverage") and first_pass_success is set to False
  * steps are re-ordered schema -> callers, top -> bottom, and renumbered (depends_on remapped)
  * a requirements.txt step bumps the pydantic pin to the Stage-2 target version

With no Nebius key or a failed call, the same deterministic builder produces the whole plan
(mode="fallback"), so the pipeline never stalls.

Plan cache: a successful Ultra reply is stored under data/cache/plans/<hash>.json, keyed by
model + system prompt + sampling params + the exact context (source, findings, grounding).
Identical reruns cost $0; any change to the repo, Stage-2 output or grounding misses the cache.
Bypass with run_stage3(..., refresh_plan=True) or REFORGE_PLAN_CACHE=0.

Telemetry follows src/model_catalog.md and is appended to data/telemetry.jsonl (live calls only).

Endpoint entry point:
    run_stage3(ws_path, stage2, *, grounding=None, use_llm=True) -> dict
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backend.extract.harness import _discover_test_files
from src.backend.planner.grounder import ROOT, _CONFIG_REMOVED, _CONFIG_RENAMES, env_key, ground, kb_entry
from src.backend.sandbox.slicer import is_test_path

BASE_URL = env_key("NEBIUS_BASE_URL") or "https://api.tokenfactory.nebius.com/v1/"
ULTRA_MODEL = env_key("REFORGE_ULTRA_MODEL") or "nvidia/Nemotron-3-Ultra-550b-a55b"
PRICING = {"ultra": (1.00, 3.00)}            # $ / 1M tokens (in, out) — src/model_catalog.md
TELEMETRY_FILE = ROOT / "data" / "telemetry.jsonl"
PLAN_CACHE_DIR = ROOT / "data" / "cache" / "plans"
MAX_FULL_SOURCE_LINES = 400
WINDOW = 8

SYSTEM_PROMPT = """You are the refactoring architect of Reforge, an autonomous Pydantic v1 -> v2 migration agent.
You receive the broken files of a legacy repo (line-numbered source, static findings, Pydantic v2 crash messages),
its test files (READ-ONLY) and official migration guidance. Produce the exact, ordered refactor checklist that a
code-patching model will apply mechanically.

Return ONLY one JSON object, no prose, no markdown fences:
{"refactor_plan": [{"step": 1, "file": "<path from affected_files>", "lines": [<line numbers>],
  "symbols": ["<stage-2 symbol(s) fixed>"], "action": "<one-line imperative summary>",
  "before": "<exact current code, copied verbatim from the numbered source, without line numbers>",
  "after": "<exact replacement code with correct indentation>",
  "instructions": "<precise guidance for the patcher, incl. behavioural caveats>",
  "depends_on": [<earlier step numbers>], "source": "<doc url backing the change>"}],
 "notes": ["<cross-cutting risks worth checking in verification>"]}

Rules:
1. Edit ONLY files listed in affected_files. NEVER edit test files or requirements.txt (handled separately).
2. Cover EVERY finding. One step per logical edit: e.g. a whole `class Config` block and its keys is ONE step
   that lists all its lines; an import line touched for several symbols is ONE step.
3. Within a file: first a single step rewriting the pydantic import(s) to their final v2 form (add ConfigDict,
   field_validator, model_validator, ... as needed), then the remaining steps top to bottom.
4. `lines` use the ORIGINAL numbering shown in the source. List every line the edit replaces.
5. `before` must match the source exactly; `after` must be complete, runnable code: no placeholders, no "...".
6. Preserve behaviour. The read-only tests must pass unchanged under Pydantic v2: keep error messages,
   exception types, serialized output (key order, separators), and the schema keys the tests read.
7. Follow the grounding guidance and caveats; prefer the documented v2 API; cite its URL in `source`.
8. Keep the given file order (foundational schema files first); call-site steps depend_on the model steps
   they rely on.
"""


# --------------------------------------------------------------------------- #
# Context assembly
# --------------------------------------------------------------------------- #
def _read_lines(ws: Path, rel: str) -> list[str]:
    try:
        return (ws / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _numbered(lines: list[str], keep: set[int] | None = None) -> str:
    width = len(str(len(lines)))
    out, prev = [], 0
    for i, line in enumerate(lines, 1):
        if keep is not None and i not in keep:
            continue
        if keep is not None and prev and i != prev + 1:
            out.append(" " * width + " | ...")
        out.append(f"{i:>{width}} | {line}")
        prev = i
    return "\n".join(out)


def _source_view(lines: list[str], finding_lines: list[int]) -> str:
    if len(lines) <= MAX_FULL_SOURCE_LINES:
        return _numbered(lines)
    keep = set(range(1, 16))                 # module header / imports
    for ln in finding_lines:
        keep.update(range(max(1, ln - WINDOW), min(len(lines), ln + WINDOW) + 1))
    return _numbered(lines, keep)


def _compact_grounding(grounding: dict, symbols: list[str]) -> dict:
    out = {}
    for s in symbols:
        g = grounding.get("symbols", {}).get(s)
        if not g:
            continue
        out[s] = {
            "v2": g.get("v2"), "action": g.get("action"), "caveats": g.get("guidance"),
            "doc_url": g.get("doc_url"), "runtime_errors": g.get("runtime_errors", [])[:2],
            "doc_evidence": [e["text"] for e in g.get("evidence", [])[:3]],
            "before_example": next((e["code"][:800] for e in g.get("examples", {}).get("before", [])), None),
            "after_example": next((e["code"][:800] for e in g.get("examples", {}).get("after", [])), None),
        }
    return out


def build_context(ws: Path, stage2: dict, grounding: dict) -> dict:
    files_info = stage2.get("details", {}).get("files", {})
    files = []
    for rel in stage2.get("affected_files", []):
        info = files_info.get(rel, {})
        lines = _read_lines(ws, rel)
        findings = [{k: f.get(k) for k in ("line", "symbol", "kind", "code", "confirmed")}
                    for f in info.get("findings", [])]
        files.append({
            "path": rel, "order": info.get("order"), "is_model_file": info.get("is_model_file"),
            "findings": findings,
            "crashes": [{k: c.get(k) for k in ("line", "exc_type", "message")} for c in info.get("crashes", [])],
            "source": _source_view(lines, [f["line"] for f in findings if f.get("line")]),
        })
    tests = []
    for p in _discover_test_files(ws)[:3]:
        rel = p.relative_to(ws).as_posix()
        lines = _read_lines(ws, rel)
        tests.append({"path": rel, "source": _numbered(lines[:250])})
    return {
        "target_pydantic": stage2.get("details", {}).get("pydantic_version"),
        "test_status_under_v2": stage2.get("test_status"),
        "affected_files": stage2.get("affected_files", []),
        "files": files,
        "tests_readonly": tests,
        "grounding": _compact_grounding(grounding, stage2.get("breakage_symbols", [])),
    }


# --------------------------------------------------------------------------- #
# Nemotron 3 Ultra call
# --------------------------------------------------------------------------- #
def call_ultra(context: dict, *, api_key: str, model: str = ULTRA_MODEL, max_tokens: int = 16384,
               temperature: float = 0.6, timeout: float = 300.0) -> dict:
    from openai import OpenAI
    client = OpenAI(base_url=BASE_URL, api_key=api_key, timeout=timeout, max_retries=1)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
    t0 = time.perf_counter()
    res = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                         top_p=0.95, max_tokens=max_tokens)
    latency = (time.perf_counter() - t0) * 1000
    msg = res.choices[0].message
    usage = res.usage
    return {
        "content": msg.content or "",
        "reasoning": getattr(msg, "reasoning_content", None) or "",
        "finish_reason": res.choices[0].finish_reason,
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "latency_ms": round(latency, 1),
        "model": model,
    }


def extract_plan_json(text: str) -> dict | None:
    """First JSON object holding `refactor_plan` in the reply (tolerates <think>, fences, prose)."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = [fence.group(1)] if fence else []
    starts = [m.start() for m in re.finditer(r"\{", text)]
    dec = json.JSONDecoder()
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and "refactor_plan" in obj:
                return obj
        except json.JSONDecodeError:
            pass
    for s in starts:
        try:
            obj, _ = dec.raw_decode(text[s:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "refactor_plan" in obj:
            return obj
    return None


def _telemetry(call: dict | None, first_pass_success: bool) -> dict:
    tin, tout = (call or {}).get("prompt_tokens", 0), (call or {}).get("completion_tokens", 0)
    rin, rout = PRICING["ultra"]
    cost = tin / 1e6 * rin + tout / 1e6 * rout
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stage": "plan",
        "tier": "ultra",
        "model": (call or {}).get("model", ULTRA_MODEL),
        "prompt_tokens": tin,
        "completion_tokens": tout,
        "latency_ms": (call or {}).get("latency_ms", 0.0),
        "actual_cost_usd": round(cost, 7),
        "counterfactual_ultra_cost_usd": round(cost, 7),   # planner always runs on Ultra
        "savings_usd": 0.0,
        "first_pass_success": first_pass_success,
    }
    if call:
        try:
            TELEMETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with TELEMETRY_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
    return entry


# --------------------------------------------------------------------------- #
# Deterministic step builder (fallback + coverage)
# --------------------------------------------------------------------------- #
_IMPORT_RENAMES = {"validator": "field_validator", "root_validator": "model_validator",
                   "parse_obj_as": "TypeAdapter", "parse_raw_as": "TypeAdapter",
                   "schema_of": "TypeAdapter", "schema_json_of": "TypeAdapter"}


def _block_end(tree: ast.AST | None, lineno: int) -> int:
    if tree is None:
        return lineno
    for n in ast.walk(tree):
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
            if start == lineno or n.lineno == lineno:
                return n.end_lineno or lineno
    return lineno


def _config_after(src: str, lines: list[str], tree: ast.AST | None, cfg_line: int) -> tuple[str, list[str]]:
    indent = re.match(r"\s*", lines[cfg_line - 1]).group(0)
    kwargs, notes = [], []
    for n in ast.walk(tree) if tree else []:
        if isinstance(n, ast.ClassDef) and n.name == "Config" and n.lineno == cfg_line:
            for s in n.body:
                if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
                    k, v = s.targets[0].id, ast.get_source_segment(src, s.value) or "..."
                    if k in _CONFIG_REMOVED:
                        notes.append(f"{k}: {_CONFIG_REMOVED[k]}")
                        continue
                    kwargs.append(f"{_CONFIG_RENAMES.get(k, k)}={v}")
    return f"{indent}model_config = ConfigDict({', '.join(kwargs)})", notes


def _decorator_mode(code: str) -> tuple[list[str], str | None]:
    """Positional args (original source text) and 'before' when pre=True."""
    expr = code.strip().lstrip("@")
    try:
        node = ast.parse(expr).body[0].value
    except (SyntaxError, IndexError, AttributeError):
        return [], None
    args, mode = [], None
    if isinstance(node, ast.Call):
        args = [ast.get_source_segment(expr, a) or ast.unparse(a) for a in node.args]
        for kw in node.keywords:
            if kw.arg == "pre" and isinstance(kw.value, ast.Constant) and kw.value.value:
                mode = "before"
    return args, mode


def _root_after_body(func_lines: list[str], dec_after: str) -> str | None:
    """Rewrite a post @root_validator function to a mode='after' instance method."""
    if len(func_lines) < 2:
        return None
    m = re.match(r"^(\s*)def (\w+)\(\s*cls\s*,\s*(\w+)[^)]*\)\s*(->\s*[^:]+)?:\s*$", func_lines[1])
    if not m:
        return None
    indent, name, var = m.group(1), m.group(2), m.group(3)
    body = "\n".join(func_lines[2:])
    body = re.sub(rf"\b{var}\.get\(\s*[\'\"](\w+)[\'\"]\s*(?:,\s*[^()]*?)?\)", r"self.\1", body)
    body = re.sub(rf"\b{var}\[\s*[\'\"](\w+)[\'\"]\s*\]", r"self.\1", body)
    body = re.sub(rf"\breturn\s+{var}\b", "return self", body)
    if re.search(rf"\b{var}\b", body):           # other uses of the dict -> leave to the patcher
        return None
    return f"{dec_after}\n{indent}def {name}(self):\n{body}"


def _decorator_after(code: str, symbol: str, indent: str, next_line: str) -> str:
    args, mode = _decorator_mode(code)
    if symbol == "@validator":
        parts = args + ([f'mode="{mode}"'] if mode else [])
        out = f"{indent}@field_validator({', '.join(parts)})"
        if "@classmethod" not in next_line:
            out += f"\n{indent}@classmethod"
        return out
    if mode == "before":
        out = f'{indent}@model_validator(mode="before")'
        return out + ("" if "@classmethod" in next_line else f"\n{indent}@classmethod")
    return f'{indent}@model_validator(mode="after")'


_CALL_RX = {
    ".dict()": (r"\.dict\(", ".model_dump("), ".parse_obj()": (r"\.parse_obj\(", ".model_validate("),
    ".parse_raw()": (r"\.parse_raw\(", ".model_validate_json("), ".schema()": (r"\.schema\(", ".model_json_schema("),
    ".construct()": (r"\.construct\(", ".model_construct("), ".copy()": (r"\.copy\(", ".model_copy("),
    ".from_orm()": (r"\.from_orm\(", ".model_validate("), ".update_forward_refs()": (r"\.update_forward_refs\(", ".model_rebuild("),
    "__fields__": (r"\b__fields__\b", "model_fields"), "__fields_set__": (r"\b__fields_set__\b", "model_fields_set"),
}
_CALL_RX["__root__"] = (r"\.__root__\b", ".root")
_MODEL_DUMP_KW = {"include", "exclude", "by_alias", "exclude_unset", "exclude_defaults", "exclude_none"}
_TYPE_ADAPTER = {"parse_obj_as": "validate_python", "parse_raw_as": "validate_json",
                 "schema_of": "json_schema", "schema_json_of": None}


def _balanced_call(code: str, start: int) -> int | None:
    """Index just past the ')' closing the '(' at code[start]."""
    depth, quote = 0, None
    for i in range(start, len(code)):
        ch = code[i]
        if quote:
            quote = None if ch == quote and code[i - 1] != "\\" else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _call_args(call_src: str) -> tuple[list[str], list[tuple[str, str]]] | None:
    try:
        node = ast.parse(call_src.strip(), mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call):
        return None
    seg = lambda n: ast.get_source_segment(call_src.strip(), n) or ast.unparse(n)
    return [seg(a) for a in node.args], [(k.arg, seg(k.value)) for k in node.keywords if k.arg]


def _rewrite_json(code: str) -> str | None:
    """v1 .json(...) -> json.dumps(x.model_dump(mode="json", <model kw>), <json kw>): byte-identical output
    (v1 used json.dumps separators; model_dump_json() emits compact JSON)."""
    m = re.search(r"\.json\(", code)
    if not m:
        return None
    end = _balanced_call(code, m.end() - 1)
    if end is None:
        return None
    i = m.start()                                          # walk back over the receiver expression
    depth = 0
    while i > 0:
        ch = code[i - 1]
        if ch in ")]":
            depth += 1
        elif ch in "([":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and not (ch.isalnum() or ch in "_."):
            break
        i -= 1
    recv = code[i:m.start()]
    parsed = _call_args("f" + code[m.end() - 1:end])
    if not recv or parsed is None:
        return None
    _, kws = parsed
    dump = ", ".join(['mode="json"'] + [f"{k}={v}" for k, v in kws if k in _MODEL_DUMP_KW])
    rest = [f"{k}={v}" for k, v in kws if k not in _MODEL_DUMP_KW and k not in ("encoder", "models_as_dict")]
    repl = f"json.dumps({recv}.model_dump({dump})" + (", " + ", ".join(rest) if rest else "") + ")"
    return code[:i] + repl + code[end:]


def _rewrite_type_adapter(code: str, fn: str) -> str | None:
    m = re.search(rf"(?<![\w.]){fn}\(", code)
    if not m:
        return None
    end = _balanced_call(code, m.end() - 1)
    parsed = _call_args(code[m.start():end]) if end else None
    if not parsed or not parsed[0]:
        return None
    args, kws = parsed
    method = _TYPE_ADAPTER[fn]
    if method is None:                                     # schema_json_of(T) -> json.dumps(TypeAdapter(T).json_schema())
        repl = f"json.dumps(TypeAdapter({args[0]}).json_schema())"
    else:
        rest = args[1:] + [f"{k}={v}" for k, v in kws]
        repl = f"TypeAdapter({args[0]}).{method}({', '.join(rest)})"
    return code[:m.start()] + repl + code[end:]


def _call_after(code: str, symbol: str) -> str | None:
    if symbol == ".json()":
        return _rewrite_json(code)
    if symbol in _TYPE_ADAPTER:
        return _rewrite_type_adapter(code, symbol)
    if symbol == ".schema()":   # keep v1 '#/definitions/...' refs so schema-derived output is unchanged
        out = re.sub(r"\.schema\(\s*\)", '.model_json_schema(ref_template="#/definitions/{model}")', code)
        return re.sub(r"\.schema\(", ".model_json_schema(", out)
    if symbol in _CALL_RX:
        rx, rep = _CALL_RX[symbol]
        return re.sub(rx, rep, code)
    m = re.match(r"^\w+\((\w+)=\.\.\.\)$", symbol)                   # Field(regex=...) / constr(regex=...)
    if m:
        v2 = kb_entry(symbol).v2 or ""
        new = re.match(r"^\w+\((\w+)=", v2)
        if new:
            return re.sub(rf"\b{m.group(1)}\s*=", f"{new.group(1)}=", code)
    return None


def _import_after(code: str, file_symbols: set[str]) -> str | None:
    m = re.match(r"^(\s*)from pydantic import (.+)$", code.rstrip())
    if not m or "(" in m.group(2):
        return None
    names = [n.strip() for n in m.group(2).split(",") if n.strip()]
    out, settings = [], []
    for n in names:
        base = n.split(" as ")[0]
        if base == "BaseSettings":
            settings.append(n)
            continue
        out.append(_IMPORT_RENAMES.get(base, base) + (n[len(base):] if " as " in n else ""))
    if "class Config" in file_symbols and "ConfigDict" not in out:
        out.append("ConfigDict")
    if "__root__" in file_symbols and "RootModel" not in out:
        out.append("RootModel")
    out = list(dict.fromkeys(out))
    line = f"{m.group(1)}from pydantic import {', '.join(out)}" if out else ""
    if settings:
        line += ("\n" if line else "") + f"{m.group(1)}from pydantic_settings import {', '.join(settings)}"
    return line


def _root_model_after(lines: list[str], tree: ast.AST | None, ln: int) -> tuple[int, int, str | None]:
    """`class X(BaseModel): __root__: T` -> `class X(RootModel[T]): pass` when __root__ is its only member."""
    for n in ast.walk(tree) if tree else []:
        if isinstance(n, ast.ClassDef) and n.lineno < ln <= (n.end_lineno or 0):
            body = [b for b in n.body if not (isinstance(b, ast.Expr) and isinstance(getattr(b, "value", None), ast.Constant))]
            ann = next((b for b in body if isinstance(b, ast.AnnAssign) and isinstance(b.target, ast.Name)
                        and b.target.id == "__root__"), None)
            if ann is None:
                continue
            src = "\n".join(lines)
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            if len(body) != 1 or ann.value is not None:
                return start, n.end_lineno or ln, None
            indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
            typ = ast.get_source_segment(src, ann.annotation) or ast.unparse(ann.annotation)
            doc = [b for b in n.body if b not in body]
            inner = "\n".join(lines[d.lineno - 1:d.end_lineno][0:] for d in doc) if doc else f"{indent}    pass"
            decos = "\n".join(lines[start - 1:n.lineno - 1])
            head = (decos + "\n" if decos else "") + f"{indent}class {n.name}(RootModel[{typ}]):"
            return start, n.end_lineno or ln, f"{head}\n{inner}"
    return ln, ln, None


def deterministic_steps(ws: Path, rel: str, findings: list[dict], all_file_findings: list[dict],
                        grounding: dict) -> list[dict]:
    """Steps for `findings` (subset of the file's findings) built from the KB + source."""
    lines = _read_lines(ws, rel)
    src = "\n".join(lines)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    file_syms = {f["symbol"] for f in all_file_findings}
    gsyms = grounding.get("symbols", {})
    steps: list[dict] = []
    used: set[int] = set()

    def get(ln: int) -> str:
        return lines[ln - 1] if 0 < ln <= len(lines) else ""

    def mk(lns, syms, action, before, after, instr="", src_url=None):
        e = kb_entry(syms[0])
        return {"file": rel, "lines": sorted(set(lns)), "symbols": list(dict.fromkeys(syms)), "action": action,
                "before": before, "after": after or "", "instructions": instr or e.note,
                "depends_on": [], "source": src_url or gsyms.get(syms[0], {}).get("doc_url") or e.doc_url}

    has_json_import = bool(re.search(r"^\s*import\s+(?:[\w.]+\s*,\s*)*json\b|^\s*from json import", src, re.M))
    needs_json = not has_json_import and any(
        f["symbol"] in (".json()", "schema_json_of") for f in findings)
    json_added = False

    # 1. one import step per import line
    by_line: dict[int, list[dict]] = {}
    for f in findings:
        if f.get("kind") == "import":
            by_line.setdefault(f["line"], []).append(f)
    for ln, fs in sorted(by_line.items()):
        code = get(ln)
        syms = [f["symbol"] for f in fs]
        after = _import_after(code, file_syms)
        if after is not None and needs_json and not json_added:
            after, json_added = f"{re.match(r'\s*', code).group(0)}import json\n{after}", True
        steps.append(mk([ln], syms, "rewrite the pydantic import to its v2 form", code, after,
                        "Import field_validator / model_validator / ConfigDict / RootModel / TypeAdapter as needed; "
                        "BaseSettings comes from pydantic_settings."))
        used.add(ln)

    # 2. class Config blocks (absorb their key findings)
    for f in findings:
        if f["symbol"] == "class Config" and f["line"] not in used:
            start, end = f["line"], _block_end(tree, f["line"])
            after, notes = _config_after(src, lines, tree, start)
            keys = [k for k in all_file_findings if start < k["line"] <= end and k.get("kind") == "config"]
            steps.append(mk(range(start, end + 1), ["class Config"] + [k["symbol"] for k in keys],
                            "replace `class Config` with `model_config = ConfigDict(...)`",
                            "\n".join(lines[start - 1:end]), after,
                            kb_entry("class Config").note + (" Dropped: " + "; ".join(notes) if notes else "")))
            used.update(range(start, end + 1))

    # 3. everything else: validators / root models first, then one step per line (all fixes on that line)
    for f in sorted(findings, key=lambda x: x["line"]):
        ln, sym = f["line"], f["symbol"]
        if ln in used:
            continue
        code = get(ln)
        e = kb_entry(sym)
        indent = re.match(r"\s*", code).group(0)
        if sym == "__root__" and f.get("kind") == "root":
            start, end, after = _root_model_after(lines, tree, ln)
            steps.append(mk(range(start, end + 1), [sym], "convert the `__root__` model to a RootModel subclass",
                            "\n".join(lines[start - 1:end]), after,
                            "Access the wrapped value with `.root` instead of `.__root__`. " + e.note
                            + ("" if after else " The class has other members: rewrite it by hand.")))
            used.update(range(start, end + 1))
            continue
        if sym in ("@validator", "@root_validator"):
            dec_after = _decorator_after(code, sym, indent, get(ln + 1))
            if sym == "@root_validator" and "after" in dec_after:
                end = _block_end(tree, ln)
                func = lines[ln - 1:end]
                full = _root_after_body(func, dec_after)
                instr = ("mode='after' receives the validated model: take `self` instead of (cls, values), read "
                         "fields as attributes, keep the same ValueError message, `return self`. " + e.note)
                steps.append(mk(range(ln, end + 1), [sym], e.action, "\n".join(func), full,
                                instr if full else "Rewrite the function body by hand. " + instr))
                used.update(range(ln, end + 1))
            else:   # decorator line only; the body is unchanged
                steps.append(mk([ln], [sym], e.action, code, dec_after, e.note))
                used.add(ln)
            continue
        if f.get("kind") == "config":            # key outside a detected Config block
            steps.append(mk([ln], [sym], e.action, code, None))
            continue
        same_line = [x for x in findings if x["line"] == ln and x.get("kind") not in ("config", "import", "root")]
        syms = list(dict.fromkeys(x["symbol"] for x in same_line)) or [sym]
        after, failed = code, []
        for sy in sorted(syms, key=lambda x: x != ".json()"):   # .json() first: it wraps the whole receiver
            nxt = _call_after(after, sy)
            if nxt is None:
                failed.append(sy)
            else:
                after = nxt
        notes = [kb_entry(sy).note for sy in syms if kb_entry(sy).note]
        if needs_json and not json_added and "json.dumps(" in after:
            notes.insert(0, "Add `import json` at the top of the module.")
        if failed:
            notes.insert(0, f"Could not rewrite {', '.join(failed)} mechanically: apply by hand.")
        action = "; ".join(kb_entry(sy).action for sy in syms)
        steps.append(mk([ln], syms, action, code, after if after != code else None, " ".join(notes)))
        used.add(ln)
    return steps


def manifest_step(ws: Path, target: str | None, symbols: list[str]) -> list[dict]:
    req = ws / "requirements.txt"
    if not target or not req.is_file():
        return []
    lines = req.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines, 1):
        if re.match(r"^\s*pydantic(\[[^\]]*\])?\s*([<>=!~].*)?$", line.split("#", 1)[0]) and target not in line:
            after = f"pydantic=={target}"
            if "BaseSettings" in symbols and not any("pydantic-settings" in l for l in lines):
                after += "\npydantic-settings"
            return [{"file": "requirements.txt", "lines": [i], "symbols": ["pydantic"],
                     "action": f"bump pydantic to =={target}", "before": line, "after": after,
                     "instructions": "Pin the v2 release the sandbox verified against.",
                     "depends_on": [], "source": "https://docs.pydantic.dev/latest/migration/#install-pydantic-v2",
                     "origin": "manifest"}]
    return []


# --------------------------------------------------------------------------- #
# Validation / normalisation of the LLM plan
# --------------------------------------------------------------------------- #
def _covers(step: dict, line: int, symbol: str) -> bool:
    lns = step.get("lines") or []
    if line in lns:
        return True
    if len(lns) >= 2 and min(lns) <= line <= max(lns) and max(lns) - min(lns) <= 25:
        return True
    return not lns and symbol in (step.get("symbols") or [])


def rewrite_line(code: str, symbols: list[str]) -> str | None:
    """Deterministic v2 rewrite of one source line for the given call/attribute symbols."""
    after = code
    for sy in sorted(symbols, key=lambda x: x != ".json()"):   # .json() first: it wraps the receiver
        nxt = _call_after(after, sy)
        if nxt is None:
            return None
        after = nxt
    return after


_MODE_BEFORE = re.compile(r"""\s*,\s*mode\s*=\s*["']before["']""")


_JSON_IMPORTED = re.compile(r"^\s*import\s+(?:[\w.]+\s*,\s*)*json\b|^\s*from json import", re.M)


def _ensure_json_import(steps: list[dict], rel: str, lines: list[str], findings: list[dict]) -> bool:
    """Make the plan itself add `import json` (a patcher applies `after`, not prose). True if added."""
    if _JSON_IMPORTED.search("\n".join(lines)):
        return False
    file_steps = [o for o in steps if o["file"] == rel]
    if any(_JSON_IMPORTED.search(o.get("after") or "") for o in file_steps):
        return False
    imp = sorted({f["line"] for f in findings if f.get("kind") == "import"})
    target = next((o for o in file_steps if imp and imp[0] in (o.get("lines") or []) and o.get("after")), None)
    if target is not None:
        indent = re.match(r"\s*", target["after"]).group(0)
        target["after"] = f"{indent}import json\n{target['after']}"
        return True
    first = next((i for i, l in enumerate(lines, 1) if re.match(r"\s*(from\s+\S+\s+)?import\s", l)), None)
    if first is None or any(first in (o.get("lines") or []) for o in file_steps):
        return False
    steps.append({"_id": max([o.get("_id", 0) for o in steps if isinstance(o.get("_id"), int)] or [0]) + 1000,
                  "file": rel, "lines": [first], "symbols": [".json()"], "action": "add `import json`",
                  "before": lines[first - 1], "after": f"import json\n{lines[first - 1]}",
                  "instructions": "Needed by the json.dumps(...) rewrite of .json().", "depends_on": [],
                  "source": kb_entry(".json()").doc_url, "origin": "coverage"})
    return True


def apply_guardrails(steps: list[dict], ws: Path, files_info: dict) -> list[str]:
    """Correct known behaviour-changing slips in LLM steps. Returns human-readable fixes."""
    fixes: list[str] = []
    for st in steps:
        if st.get("origin") != "llm" or not st.get("after"):
            continue
        lines = _read_lines(ws, st["file"])
        findings = files_info.get(st["file"], {}).get("findings", [])
        covered = [f for f in findings if _covers(st, f["line"], f["symbol"])]
        # 1. plain @validator (no pre=True) must stay an after-validator
        for f in covered:
            if f["symbol"] == "@validator" and f.get("kind") == "decorator":
                src = lines[f["line"] - 1] if 0 < f["line"] <= len(lines) else ""
                if not re.search(r"\bpre\s*=\s*True", src) and re.search(r"field_validator\([^)]*mode\s*=\s*[\"']before", st["after"]):
                    st["after"] = re.sub(r"(field_validator\([^)]*?)" + _MODE_BEFORE.pattern, r"\1", st["after"])
                    fixes.append(f"{st['file']}:{f['line']} removed mode='before' from a plain @validator (v1 ran it after coercion)")
        # 2. .json() -> model_dump_json() changes the JSON text; use the output-preserving rewrite
        if ".model_dump_json(" in st["after"] and any(f["symbol"] == ".json()" for f in covered):
            ln = min(f["line"] for f in covered if f["symbol"] == ".json()")
            if st["lines"] == [ln] and 0 < ln <= len(lines):
                syms = [f["symbol"] for f in covered if f["line"] == ln and f.get("kind") in ("call", "attribute")]
                new = rewrite_line(lines[ln - 1], syms)
                if new and "json.dumps(" in new:
                    st["after"] = new
                    fixes.append(f"{st['file']}:{ln} .json() -> json.dumps(model_dump(mode='json')) to keep the v1 JSON text")
                    if _ensure_json_import(steps, st["file"], lines, findings):
                        fixes.append(f"{st['file']}: added `import json` to the import step")
        if fixes and fixes[-1].startswith(st["file"]):
            st["instructions"] = "[guardrail-corrected] " + st["instructions"]
    return fixes


def realign_lines(steps: list[dict], ws: Path) -> list[str]:
    """Make `lines` match where `before` really is (a patcher replacing by line number must not eat
    neighbouring code). Exact match nearest to the claimed lines wins; misses are flagged."""
    notes: list[str] = []
    cache: dict[str, list[str]] = {}
    for st in steps:
        before = (st.get("before") or "").rstrip("\n")
        if not before or not st.get("lines"):
            continue
        lines = cache.setdefault(st["file"], _read_lines(ws, st["file"]))
        lo, hi = min(st["lines"]), max(st["lines"])
        if "\n".join(lines[lo - 1:hi]) == before:
            continue
        want = before.split("\n")
        n = len(want)
        hits = [i + 1 for i in range(len(lines) - n + 1) if lines[i:i + n] == want]
        if not hits:   # tolerate indentation/whitespace drift in the model's copy
            norm = [w.strip() for w in want]
            hits = [i + 1 for i in range(len(lines) - n + 1) if [l.strip() for l in lines[i:i + n]] == norm]
        if hits:
            start = min(hits, key=lambda h: abs(h - lo))
            new = list(range(start, start + n))
            notes.append(f"{st['file']} step lines {st['lines']} -> {new} (aligned to `before`)")
            st["lines"] = new
        else:
            notes.append(f"{st['file']} lines {st['lines']}: `before` not found in source")
            st["instructions"] = "[before-not-found: locate the code manually] " + st.get("instructions", "")
    return notes


def normalise_plan(raw: dict, ws: Path, stage2: dict, grounding: dict) -> tuple[list[dict], dict]:
    affected = stage2.get("affected_files", [])
    files_info = stage2.get("details", {}).get("files", {})
    line_counts = {rel: len(_read_lines(ws, rel)) for rel in affected}
    kept, dropped = [], []
    for i, s in enumerate(raw.get("refactor_plan") or [], 1):
        if not isinstance(s, dict):
            continue
        rel = str(s.get("file", "")).replace("\\", "/").removeprefix("./")
        if rel not in affected or is_test_path(rel):
            dropped.append({"file": rel, "action": s.get("action"), "reason": "not an editable affected file"})
            continue
        lns = s.get("lines")
        if lns is None and "line" in s:
            lns = [s["line"]]
        if isinstance(lns, (int, str)):
            lns = [lns]
        clean = sorted({int(x) for x in (lns or []) if str(x).lstrip("-").isdigit() and 0 < int(x) <= line_counts[rel]})
        syms = s.get("symbols") or ([s["symbol"]] if s.get("symbol") else [])
        if isinstance(syms, str):
            syms = [syms]
        if not clean and syms:   # bad/missing line numbers -> take them from the matching findings
            clean = sorted({f["line"] for f in files_info.get(rel, {}).get("findings", [])
                            if f.get("symbol") in syms and f.get("line")})
        action = str(s.get("action") or (kb_entry(syms[0]).action if syms else "")).strip()
        if not action:
            dropped.append({"file": rel, "reason": "missing action"})
            continue
        kept.append({
            "_id": s.get("step", i), "file": rel, "lines": clean, "symbols": [str(x) for x in syms],
            "action": action, "before": str(s.get("before") or ""), "after": str(s.get("after") or ""),
            "instructions": str(s.get("instructions") or ""),
            "depends_on": [d for d in (s.get("depends_on") or []) if isinstance(d, int)],
            "source": str(s.get("source") or (kb_entry(syms[0]).doc_url if syms else "")),
            "origin": "llm",
        })

    line_fixes = realign_lines(kept, ws)
    guard_fixes = apply_guardrails(kept, ws, files_info)

    # coverage
    added, total = [], 0
    for rel in affected:
        fs = [f for f in files_info.get(rel, {}).get("findings", []) if f.get("confidence") != "low"]
        total += len(fs)
        missing = [f for f in fs if not any(st["file"] == rel and _covers(st, f["line"], f["symbol"]) for st in kept)]
        if missing:
            for st in deterministic_steps(ws, rel, missing, fs, grounding):
                st["origin"] = "coverage"
                added.append(st)
    return kept + added, {"findings": total, "auto_added": len(added), "dropped_steps": dropped,
                          "guardrail_fixes": guard_fixes, "line_fixes": line_fixes}


def order_plan(steps: list[dict], stage2: dict, manifest: list[dict]) -> list[dict]:
    order = {rel: i for i, rel in enumerate(stage2.get("affected_files", []))}
    body = sorted(steps, key=lambda s: (order.get(s["file"], 999), min(s["lines"] or [10 ** 6]),
                                        s.get("origin") != "llm"))
    final = manifest + body
    id_map = {}
    for n, s in enumerate(final, 1):
        if "_id" in s:
            id_map[s.pop("_id")] = n
        s["step"] = n
    for s in final:
        s["depends_on"] = sorted({id_map[d] for d in s.get("depends_on", []) if d in id_map and id_map[d] < s["step"]})
    keys = ("step", "file", "lines", "symbols", "action", "before", "after", "instructions", "depends_on", "source", "origin")
    return [{k: s.get(k) for k in keys} for s in final]


# --------------------------------------------------------------------------- #
# Plan cache
# --------------------------------------------------------------------------- #
def _cache_enabled() -> bool:
    return (env_key("REFORGE_PLAN_CACHE") or "1").lower() not in ("0", "false", "no", "off")


def plan_cache_key(context: dict, model: str, llm_kwargs: dict) -> str:
    blob = json.dumps({"model": model, "system": SYSTEM_PROMPT, "params": llm_kwargs, "context": context},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _cache_load(key: str) -> dict | None:
    f = PLAN_CACHE_DIR / f"{key}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def _cache_save(key: str, call: dict) -> None:
    try:
        PLAN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {**call, "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        (PLAN_CACHE_DIR / f"{key}.json").write_text(json.dumps(rec), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_stage3(ws_path: str | Path, stage2: dict, *, grounding: dict | None = None, use_llm: bool = True,
               api_key: str | None = None, refresh_plan: bool = False, **llm_kwargs: Any) -> dict[str, Any]:
    ws = Path(ws_path).resolve()
    grounding = grounding if grounding is not None else ground(stage2)
    target = stage2.get("details", {}).get("pydantic_version") or grounding.get("pydantic_target")
    manifest = manifest_step(ws, target, stage2.get("breakage_symbols", []))
    files_info = stage2.get("details", {}).get("files", {})

    key = api_key or env_key("nebius_token_factory", "NEBIUS_API_KEY")
    call, error, raw, cache_hit, cache_key = None, None, None, False, None
    plannable = [rel for rel in stage2.get("affected_files", [])
                 if (ws / rel).is_file() and not is_test_path(rel) and files_info.get(rel, {}).get("findings")]
    if use_llm and not plannable:
        use_llm, error = False, "nothing to plan: no findings in editable repo files (Ultra call skipped)"
    if use_llm and stage2.get("affected_files"):
        context = build_context(ws, stage2, grounding)
        model = llm_kwargs.get("model", ULTRA_MODEL)
        cache_key = plan_cache_key(context, model, {k: v for k, v in llm_kwargs.items() if k not in ("timeout",)})
        if _cache_enabled() and not refresh_plan:
            cached = _cache_load(cache_key)
            if cached:
                call, cache_hit = cached, True
                raw = extract_plan_json(call.get("content", "")) or extract_plan_json(call.get("reasoning", ""))
        if raw is None and key:
            call, cache_hit = None, False
            try:
                call = call_ultra(context, api_key=key, **llm_kwargs)
                raw = extract_plan_json(call["content"]) or extract_plan_json(call["reasoning"])
                if raw is None:
                    error = f"no JSON plan in reply (finish_reason={call.get('finish_reason')})"
                elif _cache_enabled():
                    _cache_save(cache_key, call)
            except Exception as e:  # network, auth, quota, timeout
                error = f"{type(e).__name__}: {str(e)[:300]}"
        elif raw is None:
            error = "no Nebius key (nebius_token_factory / NEBIUS_API_KEY)"
    elif use_llm and not key:
        error = "no Nebius key (nebius_token_factory / NEBIUS_API_KEY)"

    if raw is not None:
        steps, coverage = normalise_plan(raw, ws, stage2, grounding)
        mode = "llm"
        notes = [str(n) for n in raw.get("notes", []) if n][:10]
    else:
        steps, notes, mode = [], [], "fallback"
        total = 0
        for rel in stage2.get("affected_files", []):
            fs = [f for f in files_info.get(rel, {}).get("findings", []) if f.get("confidence") != "low"]
            total += len(fs)
            for st in deterministic_steps(ws, rel, fs, fs, grounding):
                st["origin"] = "fallback"
                steps.append(st)
        coverage = {"findings": total, "auto_added": 0, "dropped_steps": []}

    first_pass = (mode == "llm" and coverage["auto_added"] == 0 and not coverage["dropped_steps"]
                  and not coverage.get("guardrail_fixes"))
    telemetry = _telemetry(None if cache_hit else call, first_pass)
    if cache_hit:   # no dispatch -> no spend; report what the original call cost
        telemetry.update(cached=True, cached_at=call.get("cached_at"),
                         original_cost_usd=round(call.get("prompt_tokens", 0) / 1e6 * PRICING["ultra"][0]
                                                 + call.get("completion_tokens", 0) / 1e6 * PRICING["ultra"][1], 7))
    plan = order_plan(steps, stage2, manifest)
    return {
        "refactor_plan": plan,
        "notes": notes,
        "planner": {
            "mode": mode, "model": ULTRA_MODEL, "error": error, "first_pass_success": first_pass,
            "coverage": coverage, "finish_reason": (call or {}).get("finish_reason"), "telemetry": telemetry,
            "cache": {"hit": cache_hit, "key": cache_key},
        },
        "grounding": grounding,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 3: ground + plan the v1 -> v2 refactor")
    ap.add_argument("ws_path")
    ap.add_argument("stage2_json", help="file holding the Stage-2 payload (or the full endpoint response)")
    ap.add_argument("--no-llm", action="store_true", help="deterministic plan only")
    ap.add_argument("--brief", action="store_true", help="only file/action per step")
    ap.add_argument("--refresh-plan", action="store_true", help="ignore the plan cache (new Ultra call)")
    a = ap.parse_args(argv)
    data = json.loads(Path(a.stage2_json).read_text(encoding="utf-8"))
    out = run_stage3(a.ws_path, data.get("stage2", data), use_llm=not a.no_llm, refresh_plan=a.refresh_plan)
    if a.brief:
        out = {"refactor_plan": [{"file": s["file"], "action": s["action"]} for s in out["refactor_plan"]],
               "planner": {k: out["planner"][k] for k in ("mode", "error", "first_pass_success", "coverage", "cache")}}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":   # run from the Reforge root: python -m src.backend.planner.planner workspace/<repo> out.json
    raise SystemExit(main())
