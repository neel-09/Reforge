"""
sandbox/slicer.py — Reforge Stage 2 (static half): AST symbol extraction + merge.

Scans the workspace repo WITHOUT executing it and flags Pydantic v1 constructs that
break or are deprecated under Pydantic v2:
  * v1-only imports      validator, root_validator, BaseSettings, parse_obj_as, pydantic.generics, ...
  * class configuration  `class Config`, v1 config keys (orm_mode, anystr_strip_whitespace, ...), `__root__`
  * decorators           @validator, @root_validator
  * deprecated calls     .dict(), .json(), .parse_obj(), .schema(), .from_orm(), __fields__, Field(regex=...), ...

Builds the local import graph and merges the static scan with sandbox.py's dynamic
crash report into the Stage-2 payload, ordered schema files -> callers -> tests.

Endpoint entry point:
    run_stage2(ws_path, baseline=None, target_version=None) -> dict
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

# --------------------------------------------------------------------------- #
# Rule tables
# --------------------------------------------------------------------------- #
# dot-dirs (.git, .venv, .harness_venv, .sandbox_venv, .pytest_cache, ...) are always skipped
IGNORE_DIRS = frozenset({"venv", "env", "__pycache__", "node_modules", "build", "dist", "site-packages"})

MODEL_ROOTS = frozenset({"BaseModel", "BaseSettings", "GenericModel"})

V1_IMPORT_NAMES = frozenset({
    "validator", "root_validator", "BaseSettings", "BaseConfig", "Extra",
    "parse_obj_as", "parse_raw_as", "parse_file_as", "schema_of", "schema_json_of",
    "validate_model", "create_model_from_typeddict", "create_model_from_namedtuple",
    "ConstrainedStr", "ConstrainedInt", "ConstrainedFloat", "ConstrainedDecimal",
    "ConstrainedList", "ConstrainedSet", "ConstrainedFrozenSet", "ConstrainedBytes",
    "ConstrainedDate", "PyObject", "NoneStr", "NoneBytes", "StrBytes", "NoneStrBytes",
    "stricturl", "Required", "compiled",
})
V1_MODULES = frozenset({
    "pydantic.generics", "pydantic.error_wrappers", "pydantic.class_validators",
    "pydantic.env_settings", "pydantic.typing", "pydantic.utils", "pydantic.datetime_parse",
    "pydantic.json", "pydantic.schema", "pydantic.decorator", "pydantic.tools", "pydantic.parse",
})
V1_MODULE_NAMES = {
    "pydantic.fields": frozenset({"ModelField", "ModelPrivateAttr", "Undefined", "SHAPE_LIST"}),
    "pydantic.main": frozenset({"ModelMetaclass"}),
    "pydantic.config": frozenset({"BaseConfig", "Extra"}),
}
V1_CONFIG_KEYS = frozenset({
    "orm_mode", "allow_population_by_field_name", "anystr_lower", "anystr_upper",
    "anystr_strip_whitespace", "min_anystr_length", "max_anystr_length", "validate_all",
    "allow_mutation", "schema_extra", "underscore_attrs_are_private", "smart_union",
    "fields", "json_encoders", "copy_on_model_validation", "keep_untouched", "getter_dict",
    "json_loads", "json_dumps", "error_msg_templates", "post_init_call",
})
FIELD_V1_KWARGS = frozenset({"regex", "min_items", "max_items", "unique_items", "const", "allow_mutation", "env"})
CON_TYPE_FUNCS = frozenset({"constr", "conlist", "conset", "confrozenset", "conbytes"})
CON_V1_KWARGS = frozenset({"regex", "min_items", "max_items", "unique_items", "curtail_length"})
V1_FUNCS = frozenset({
    "parse_obj_as", "parse_raw_as", "parse_file_as", "schema_of", "schema_json_of",
    "validate_model", "create_model_from_typeddict", "create_model_from_namedtuple",
})
CLASSMETHODS_STRONG = frozenset({"parse_obj", "parse_raw", "parse_file", "from_orm", "update_forward_refs", "schema_json"})
CLASSMETHODS_WEAK = frozenset({"construct", "schema", "validate"})          # only with a known model receiver
INSTANCE_FACTORIES = frozenset({"parse_obj", "parse_raw", "parse_file", "from_orm", "construct", "copy",
                                "model_validate", "model_validate_json", "model_construct"})
COPY_V1_KWARGS = frozenset({"update", "include", "exclude"})
V1_ATTRS = frozenset({"__fields__", "__fields_set__", "__config__", "__root__"})

_IMPORT_SYMBOL_ALIASES = {"validator": "@validator", "root_validator": "@root_validator"}
KIND_WEIGHT = {"config": 5, "root": 5, "decorator": 4, "import": 4, "field_kwarg": 3, "call": 2, "attribute": 2}


def canonical_symbol(name: str) -> str:
    """Single symbol vocabulary shared with sandbox.py."""
    return _IMPORT_SYMBOL_ALIASES.get(name, name)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    col: int
    symbol: str
    kind: str          # import | config | root | decorator | call | field_kwarg | attribute
    confidence: str    # high | medium | low
    code: str


@dataclass
class StaticReport:
    findings: list[Finding] = field(default_factory=list)
    graph: dict[str, list[str]] = field(default_factory=dict)   # file -> local files it imports
    model_files: set[str] = field(default_factory=set)
    modules: dict[str, str] = field(default_factory=dict)        # file -> dotted module (repo root on sys.path)
    parse_errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"findings": [asdict(f) for f in self.findings], "graph": self.graph,
                "model_files": sorted(self.model_files), "parse_errors": self.parse_errors}


@dataclass
class _Parsed:
    rel: str
    tree: ast.Module
    lines: list[str]
    class_bases: dict[str, list[str]]
    imports_pydantic: bool
    deps: set[str]


# --------------------------------------------------------------------------- #
# Repo helpers (shared with sandbox.py)
# --------------------------------------------------------------------------- #
def iter_py_files(repo: Path) -> Iterator[tuple[Path, str]]:
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in IGNORE_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            if name.endswith(".py"):
                p = Path(root) / name
                yield p, p.relative_to(repo).as_posix()


def module_name(rel: str) -> str:
    """Dotted name with the repo root on sys.path (how harness/pytest import it: `src.schema`)."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_aliases(rel: str) -> list[str]:
    name = module_name(rel)
    out = [name] if name else []
    if name.startswith("src."):          # src-layout packages are also importable without the prefix
        out.append(name[4:])
    return out


def discover_modules(repo: str | Path) -> dict[str, str]:
    repo = Path(repo).resolve()
    return {rel: module_name(rel) for _, rel in iter_py_files(repo) if module_name(rel)}


def is_test_path(rel: str) -> bool:
    """Same rule as extract.harness._discover_test_files (+ conftest / tests dirs)."""
    p = Path(rel)
    name = p.name.lower()
    return (name.startswith("test") or name.endswith("_test.py") or "spec" in name or name == "conftest.py"
            or any(part in {"tests", "test", "testing"} for part in p.parts[:-1]))


def _tail_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_pydantic_module(mod: str | None) -> bool:
    return bool(mod) and (mod == "pydantic" or mod.startswith("pydantic."))


def _import_targets(node: ast.Import | ast.ImportFrom, mod: str, is_pkg: bool) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if node.level:
        base = mod.split(".") if mod else []
        if not is_pkg:
            base = base[:-1]
        if node.level > 1:
            base = base[: max(0, len(base) - (node.level - 1))]
        prefix = ".".join(base + ([node.module] if node.module else []))
    else:
        prefix = node.module or ""
    out = [prefix] if prefix else []
    out += [f"{prefix}.{a.name}" if prefix else a.name for a in node.names]
    return out


def _assign_names(stmt: ast.stmt) -> list[str]:
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)]
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id]
    return []


# --------------------------------------------------------------------------- #
# Per-file scanner
# --------------------------------------------------------------------------- #
class _Scanner(ast.NodeVisitor):
    def __init__(self, pf: _Parsed, models: set[str], related: bool):
        self.pf, self.models, self.related = pf, models, related
        self.mod_aliases: set[str] = set()      # local names bound to the `pydantic` module
        self.names: dict[str, str] = {}         # local name -> original pydantic name
        self.cls_stack: list[str] = []
        self.var_scopes: list[set[str]] = []    # variables known to hold model instances/classes
        self.findings: list[Finding] = []
        for n in ast.walk(pf.tree):             # pre-pass so aliases resolve regardless of order
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name == "pydantic":
                        self.mod_aliases.add(a.asname or "pydantic")
                    elif a.name.startswith("pydantic.") and not a.asname:
                        self.mod_aliases.add("pydantic")
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module in (
                    "pydantic", "pydantic.fields", "pydantic.types", "pydantic.class_validators"):
                for a in n.names:
                    self.names[a.asname or a.name] = a.name
        self.var_scopes.append(self._collect_vars(pf.tree.body, ()))

    # ---- helpers ---------------------------------------------------------
    def _add(self, node: ast.AST, symbol: str, kind: str, conf: str = "high") -> None:
        line = getattr(node, "lineno", 0)
        code = self.pf.lines[line - 1].strip()[:200] if 0 < line <= len(self.pf.lines) else ""
        self.findings.append(Finding(self.pf.rel, line, getattr(node, "col_offset", 0), symbol, kind, conf, code))

    def _resolve(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in self.mod_aliases:
            return node.attr
        return None

    def _is_model_class(self, name: str | None) -> bool:
        return bool(name) and name in self.models and name not in MODEL_ROOTS

    def _is_model_expr(self, e: ast.AST | None) -> bool:
        """`Model(...)`, `Model.parse_obj(...)`, `known_var.copy(...)`."""
        if not isinstance(e, ast.Call):
            return False
        f = e.func
        if isinstance(f, ast.Name):
            return self._is_model_class(f.id)
        if isinstance(f, ast.Attribute) and f.attr in INSTANCE_FACTORIES:
            return self._is_model_receiver(f.value)
        return False

    def _collect_vars(self, body: list[ast.stmt], args: Any) -> set[str]:
        """Lightweight type inference for one scope."""
        found: set[str] = set()
        for a in args:
            if self._is_model_class(_tail_name(a.annotation)):
                found.add(a.arg)
        self.var_scopes.append(found)            # visible while inferring later statements
        try:
            for stmt in body:
                for n in ast.walk(stmt):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and n is not stmt:
                        continue
                    if isinstance(n, ast.Assign) and self._is_model_expr(n.value):
                        found.update(t.id for t in n.targets if isinstance(t, ast.Name))
                    elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and (
                            self._is_model_class(_tail_name(n.annotation)) or self._is_model_expr(n.value)):
                        found.add(n.target.id)
        finally:
            self.var_scopes.pop()
        return found

    def _is_model_receiver(self, recv: ast.AST) -> bool:
        if isinstance(recv, ast.Name):
            if recv.id in self.models or any(recv.id in s for s in self.var_scopes):
                return True
            return recv.id in ("self", "cls") and bool(self.cls_stack) and self.cls_stack[-1] in self.models
        if isinstance(recv, ast.Call):
            return self._is_model_expr(recv) or _tail_name(recv.func) in self.models
        return False

    # ---- imports ---------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name in V1_MODULES:
                self._add(node, a.name, "import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        m = node.module or ""
        if node.level or not _is_pydantic_module(m) or m.startswith("pydantic.v1"):
            return
        for a in node.names:
            if m in V1_MODULES or a.name in V1_MODULE_NAMES.get(m, ()) or (m == "pydantic" and a.name in V1_IMPORT_NAMES):
                self._add(node, canonical_symbol(a.name), "import")

    # ---- class config / __root__ ----------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._is_model_class(node.name):
            for kw in node.keywords:
                if kw.arg in V1_CONFIG_KEYS:
                    self._add(node, kw.arg, "config")
            for stmt in node.body:
                if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
                    self._add(stmt, "class Config", "config")
                    for s in stmt.body:
                        for key in _assign_names(s):
                            if key in V1_CONFIG_KEYS:
                                self._add(s, key, "config")
                if "__root__" in _assign_names(stmt):
                    self._add(stmt, "__root__", "root")
        self.cls_stack.append(node.name)
        self.generic_visit(node)
        self.cls_stack.pop()

    # ---- decorators + function scopes ------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for d in node.decorator_list:
            orig = self._resolve(d.func if isinstance(d, ast.Call) else d)
            if orig in ("validator", "root_validator"):
                self._add(d, "@" + orig, "decorator")
        a = node.args
        self.var_scopes.append(self._collect_vars(node.body, [*a.posonlyargs, *a.args, *a.kwonlyargs]))
        self.generic_visit(node)
        self.var_scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # ---- calls -----------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        orig = self._resolve(node.func)
        if orig == "Field":
            for kw in node.keywords:
                if kw.arg in FIELD_V1_KWARGS:
                    self._add(node, f"Field({kw.arg}=...)", "field_kwarg")
        elif orig in CON_TYPE_FUNCS:
            for kw in node.keywords:
                if kw.arg in CON_V1_KWARGS:
                    self._add(node, f"{orig}({kw.arg}=...)", "field_kwarg")
        elif orig in V1_FUNCS:
            self._add(node, orig, "call")
        elif isinstance(node.func, ast.Attribute):
            self._method_call(node, node.func)
        self.generic_visit(node)

    def _method_call(self, node: ast.Call, f: ast.Attribute) -> None:
        attr, model_recv = f.attr, self._is_model_receiver(f.value)
        kwargs = {k.arg for k in node.keywords if k.arg}
        conf: str | None = None
        if attr in CLASSMETHODS_STRONG:
            conf = "high" if model_recv else ("medium" if self.related else None)
        elif attr in CLASSMETHODS_WEAK:
            conf = "high" if model_recv else None
        elif attr == "dict":
            conf = "high" if model_recv else ("medium" if self.related else None)
        elif attr == "json":   # requests/httpx `.json()` takes no kwargs; v1 model .json(sort_keys=...) does
            conf = "high" if model_recv else (("medium" if kwargs else "low") if self.related else None)
        elif attr == "copy" and COPY_V1_KWARGS & kwargs:
            conf = "high" if model_recv else ("medium" if self.related else None)
        if conf:
            self._add(node, f".{attr}()", "call", conf)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in V1_ATTRS and self.related:
            self._add(node, node.attr, "attribute", "high" if self._is_model_receiver(node.value) else "medium")
        self.generic_visit(node)


# --------------------------------------------------------------------------- #
# Repo scan
# --------------------------------------------------------------------------- #
def scan_repo(repo: str | Path) -> StaticReport:
    repo = Path(repo).resolve()
    report = StaticReport()
    files = list(iter_py_files(repo))
    report.modules = {rel: module_name(rel) for _, rel in files}
    mod_to_file: dict[str, str] = {}
    for _, rel in files:
        for alias in _module_aliases(rel):
            mod_to_file.setdefault(alias, rel)

    parsed: dict[str, _Parsed] = {}
    for path, rel in files:
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=rel)
        except (SyntaxError, ValueError) as e:
            report.parse_errors[rel] = f"{type(e).__name__}: {e}"
            continue
        mod, is_pkg = report.modules[rel], rel.endswith("__init__.py")
        bases: dict[str, list[str]] = {}
        imports_pyd, deps = False, set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                bases[n.name] = [b for b in map(_tail_name, n.bases) if b]
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                if isinstance(n, ast.Import):
                    imports_pyd |= any(_is_pydantic_module(a.name) for a in n.names)
                elif not n.level:
                    imports_pyd |= _is_pydantic_module(n.module)
                for t in _import_targets(n, mod, is_pkg):
                    target = mod_to_file.get(t)
                    if target and target != rel:
                        deps.add(target)
        parsed[rel] = _Parsed(rel, tree, src.splitlines(), bases, imports_pyd, deps)

    # Model classes: fixpoint over inheritance (by name, repo-wide).
    models = set(MODEL_ROOTS)
    changed = True
    while changed:
        changed = False
        for pf in parsed.values():
            for cls, bs in pf.class_bases.items():
                if cls not in models and any(b in models for b in bs):
                    models.add(cls)
                    changed = True
    report.model_files = {pf.rel for pf in parsed.values()
                          if any(c in models and c not in MODEL_ROOTS for c in pf.class_bases)}

    for pf in parsed.values():
        related = pf.imports_pydantic or bool(pf.deps & report.model_files) or pf.rel in report.model_files
        sc = _Scanner(pf, models, related)
        sc.visit(pf.tree)
        report.findings.extend(sorted(set(sc.findings), key=lambda f: (f.line, f.col, f.symbol)))
        report.graph[pf.rel] = sorted(pf.deps)
    return report


# --------------------------------------------------------------------------- #
# Merge (static + dynamic) -> prioritized Stage-2 payload
# --------------------------------------------------------------------------- #
def _depths(graph: dict[str, list[str]]) -> dict[str, int]:
    """0 = imports no local module (foundational schema); higher = further up the call chain."""
    depth: dict[str, int] = {}
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10_000))

    def visit(n: str, stack: set[str]) -> int:
        if n in depth:
            return depth[n]
        if n in stack:  # import cycle
            return 0
        stack.add(n)
        d = max((visit(m, stack) + 1 for m in graph.get(n, ())), default=0)
        stack.discard(n)
        depth[n] = d
        return d

    for node in graph:
        visit(node, set())
    return depth


def merge_reports(static: StaticReport, dynamic: Any = None) -> dict[str, Any]:
    dyn = dynamic.to_dict() if hasattr(dynamic, "to_dict") else (dynamic or {})
    per_file: dict[str, dict[str, list]] = defaultdict(lambda: {"findings": [], "crashes": []})
    dyn_files, dyn_syms, stat_syms = Counter(), Counter(), Counter()

    unattributed = []
    for c in dyn.get("crashes", []):
        dyn_syms.update(c.get("symbols", []))
        f = c.get("file")
        if f and f not in static.modules:            # crash outside the repo's own .py files
            unattributed.append({k: c.get(k) for k in ("test_id", "exc_type", "message", "file", "line")})
            continue
        if f:
            dyn_files[f] += 1
            if len(per_file[f]["crashes"]) < 20:
                per_file[f]["crashes"].append({k: c.get(k) for k in ("test_id", "exc_type", "message", "line", "symbols")})

    for fnd in static.findings:
        confirmed = fnd.symbol in dyn_syms              # symbol itself seen in a v2 crash
        if fnd.confidence == "low" and not (confirmed or fnd.file in dyn_files):
            continue
        stat_syms[fnd.symbol] += 1
        d = asdict(fnd)
        d.pop("file")
        d["confirmed"] = confirmed
        per_file[fnd.file]["findings"].append(d)

    depth = _depths(static.graph)

    def weight(rel: str) -> int:
        return sum(KIND_WEIGHT.get(x["kind"], 1) for x in per_file[rel]["findings"])

    affected = sorted(per_file, key=lambda r: (
        is_test_path(r),                 # tests last
        depth.get(r, 0),                 # foundational schemas -> callers
        r not in static.model_files,     # model-defining files first within a layer
        -dyn_files[r], -weight(r), r,
    ))
    symbols = sorted(set(stat_syms) | set(dyn_syms),
                     key=lambda s: (s not in dyn_syms, -(dyn_syms[s] * 10 + stat_syms[s]), s))

    return {
        "test_status": dyn.get("test_status", "not_run"),
        "breakage_symbols": symbols,
        "affected_files": affected,
        "details": {
            "files": {
                r: {"order": i, "depth": depth.get(r, 0), "is_model_file": r in static.model_files,
                    "dynamic_hits": dyn_files[r], **per_file[r]}
                for i, r in enumerate(affected)
            },
            "test_counts": dyn.get("counts", {}),
            "newly_broken": dyn.get("newly_broken", []),
            "outcomes": dyn.get("outcomes", {}),
            "sandbox_ok": dyn.get("setup_ok"),
            "pydantic_version": dyn.get("pydantic_version"),
            "sandbox_python": dyn.get("python"),
            "parse_errors": static.parse_errors,
            "unattributed_crashes": unattributed[:20],
            "sandbox_log_tail": (dyn.get("log_tail") or "")[-1500:] if dyn.get("exit_code") not in (None, 0, 1, 5) else "",
            "import_graph": static.graph,
        },
    }


def run_stage2(ws_path: str | Path, *, baseline: dict | None = None, target_version: str | None = None,
               run_sandbox: bool = True, **sandbox_kwargs: Any) -> dict[str, Any]:
    """Stage-2 entry point for the endpoint: static scan + dynamic break run -> merged payload.

    baseline:       Stage-1 `run_harness` result (its `outcomes` mark newly broken tests).
    target_version: Stage-1 `scan.latest_version` to pin the v2 release; default `pydantic>=2,<3`.
    """
    static = scan_repo(ws_path)
    dynamic: Any = None
    if run_sandbox:
        from src.backend.sandbox.sandbox import run_break
        try:
            dynamic = run_break(ws_path, baseline=baseline, target_version=target_version, **sandbox_kwargs)
        except Exception as e:  # never let the sandbox kill the static result
            dynamic = {"test_status": f"sandbox_error: {type(e).__name__}: {e}", "crashes": [], "setup_ok": False}
    return merge_reports(static, dynamic)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 2: static Pydantic v1 scan (+ sandbox merge)")
    ap.add_argument("ws_path")
    ap.add_argument("--static-only", action="store_true")
    ap.add_argument("--raw", action="store_true", help="raw static findings only")
    ap.add_argument("--brief", action="store_true", help="only test_status / breakage_symbols / affected_files")
    a = ap.parse_args(argv)
    if a.raw:
        out: Any = scan_repo(a.ws_path).to_dict()
    else:
        out = run_stage2(a.ws_path, run_sandbox=not a.static_only)
        if a.brief:
            out = {k: out[k] for k in ("test_status", "breakage_symbols", "affected_files")}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":   # run from the Reforge root: python -m src.backend.sandbox.slicer workspace/<repo>
    raise SystemExit(main())
