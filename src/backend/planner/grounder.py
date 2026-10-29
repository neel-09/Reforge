"""
planner/grounder.py — Reforge Stage 3 (research half): ground each broken symbol in the
official Pydantic v2 migration docs.

Input:  the Stage-2 payload (breakage_symbols + per-file findings/crash messages).
Output: per-symbol v2 replacement, doc evidence lines, and before/after code examples.

How it works
  1. Every Stage-2 symbol is mapped to its v2 counterpart via SYMBOL_KB (curated from
     https://docs.pydantic.dev/latest/migration/). The KB supplies exact search terms and
     a reference before/after pair so grounding still works when Tavily is unreachable.
  2. Symbols are grouped by topic (validators, config, BaseModel methods, Field, ...) and
     each group becomes ONE targeted Tavily query built from the exact v1 -> v2 symbols
     plus the runtime crash message, restricted to docs.pydantic.dev.
  3. The markdown of each hit is split into sections; for each symbol the best-matching
     section yields evidence lines (table rows / bullets naming the symbol) and code
     blocks, classified as `before` (v1) or `after` (v2) examples.
  4. Tavily responses are cached under data/cache/grounding/ (saves credits, makes demos
     deterministic).

Entry point:
    ground(stage2, *, max_queries=6, use_cache=True) -> dict
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]          # src/backend: data/ (caches, telemetry, audit) lives here
REPO_ROOT = Path(__file__).resolve().parents[3]     # Reforge/: .env lives here
CACHE_DIR = ROOT / "data" / "cache" / "grounding"
TAVILY_URL = "https://api.tavily.com/search"
DOC_DOMAIN = "docs.pydantic.dev"
MIGRATION_URL = "https://docs.pydantic.dev/latest/migration/"
MAX_QUERY_CHARS = 400        # Tavily guidance: keep queries under ~400 chars
MAX_CODE_CHARS = 1500

try:  # the endpoint never calls load_dotenv; keys live in Reforge/.env
    from dotenv import load_dotenv
    for _env in (REPO_ROOT / ".env", ROOT / ".env"):
        if _env.is_file():
            load_dotenv(_env, override=False)
except ImportError:
    pass


def env_key(*names: str) -> str | None:
    for n in names:
        for k in (n, n.upper(), n.lower()):
            v = os.environ.get(k)
            if v:
                return v.strip().strip("\"'")
    return None


# --------------------------------------------------------------------------- #
# Symbol knowledge base (vocabulary shared with sandbox/slicer.py)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class KBEntry:
    topic: str
    v2: str | None             # replacement symbol (None = removed / no direct equivalent)
    action: str                # imperative fix, used by the planner's fallback/coverage steps
    match: tuple[str, ...]     # strings that identify the v1 symbol in docs
    v2_match: tuple[str, ...]  # strings that identify the v2 symbol in docs
    anchor: str = ""           # migration-guide anchor
    note: str = ""             # behavioural caveats the plan must respect
    before: str = ""
    after: str = ""

    @property
    def doc_url(self) -> str:
        return MIGRATION_URL + (f"#{self.anchor}" if self.anchor else "")


TOPIC_TITLES = {
    "validators": "validators",
    "config": "model config (class Config -> model_config = ConfigDict)",
    "model_methods": "BaseModel method renames",
    "field": "pydantic.Field changes",
    "root": "custom root types (__root__ -> RootModel)",
    "settings": "BaseSettings moved to pydantic-settings",
    "functions": "parse_obj_as / schema_of -> TypeAdapter",
    "generics": "GenericModel",
    "types": "constrained types",
    "imports": "moved and removed imports",
    "other": "migration",
}


def _method(v1: str, v2: str, action: str = "", note: str = "", before: str = "", after: str = "",
            anchor: str = "changes-to-pydanticbasemodel") -> KBEntry:
    return KBEntry("model_methods", v2, action or f"replace .{v1}() with .{v2}()",
                   (f".{v1}(", f"{v1}()"), (f"{v2}(", f"{v2}()"), anchor, note,
                   before or f"obj.{v1}()", after or f"obj.{v2}()")


SYMBOL_KB: dict[str, KBEntry] = {
    # ---- validators ------------------------------------------------------
    "@validator": KBEntry(
        "validators", "@field_validator",
        "replace @validator with @field_validator, add @classmethod below it, and import field_validator; "
        "use mode='before' ONLY when the v1 decorator had pre=True",
        ("@validator", "validator"), ("field_validator",), "changes-to-validators",
        "A plain @validator (no pre=True) is an AFTER validator: it runs after type coercion and config "
        "transforms (str_to_lower, str_strip_whitespace, ...), so it must become @field_validator(...) with the "
        "default mode ('after'); adding mode='before' changes the order and the results. "
        "pre=True -> mode='before'; each_item has no equivalent (use Annotated on the item type); "
        "always=True -> Field(validate_default=True); signature (cls, v, values) -> (cls, v, info: ValidationInfo) "
        "with info.data; the `field`/`config` args are gone.",
        '@validator("x", pre=True)\ndef check_x(cls, v):\n    return v',
        '@field_validator("x", mode="before")\n@classmethod\ndef check_x(cls, v):\n    return v'),
    "@root_validator": KBEntry(
        "validators", "@model_validator",
        "replace @root_validator with @model_validator(mode='after') taking self and returning self "
        "(or mode='before' + @classmethod for pre=True), and import model_validator",
        ("@root_validator", "root_validator"), ("model_validator",), "changes-to-validators",
        "post root validators received a dict of *validated* values -> mode='after' gets the model instance: "
        "read fields as attributes and `return self`. pre=True -> mode='before' classmethod receiving raw input. "
        "Raising ValueError still surfaces as ValidationError (a ValueError subclass).",
        "@root_validator\ndef check(cls, values):\n    if values.get('a') > values.get('b'):\n"
        "        raise ValueError('a > b')\n    return values",
        "@model_validator(mode='after')\ndef check(self):\n    if self.a > self.b:\n"
        "        raise ValueError('a > b')\n    return self"),
    # ---- config ----------------------------------------------------------
    "class Config": KBEntry(
        "config", "model_config = ConfigDict(...)",
        "replace the nested `class Config` with `model_config = ConfigDict(...)` (renaming v1 keys) "
        "and import ConfigDict",
        ("class Config", "Config"), ("model_config", "ConfigDict"), "changes-to-config",
        "Keys keep their meaning unless renamed: anystr_strip_whitespace -> str_strip_whitespace, "
        "orm_mode -> from_attributes, allow_population_by_field_name -> populate_by_name, "
        "schema_extra -> json_schema_extra, validate_all -> validate_default. extra='forbid' is unchanged.",
        "class Model(BaseModel):\n    class Config:\n        orm_mode = True\n        extra = 'forbid'",
        "class Model(BaseModel):\n    model_config = ConfigDict(from_attributes=True, extra='forbid')"),
    "__root__": KBEntry(
        "root", "RootModel", "replace the `__root__` field with a RootModel[...] subclass and use `.root`",
        ("__root__",), ("RootModel",), "changes-to-pydanticbasemodel",
        "Access the value via `.root`; RootModel does not support arbitrary_types_allowed.",
        "class Tags(BaseModel):\n    __root__: list[str]", "class Tags(RootModel[list[str]]):\n    pass"),
    # ---- BaseModel methods ----------------------------------------------
    ".dict()": _method("dict", "model_dump", note="include/exclude/by_alias/exclude_unset/exclude_none keep working."),
    ".json()": _method(
        "json", "model_dump_json",
        action="replace obj.json(...) with json.dumps(obj.model_dump(mode=\"json\", <model kwargs>), <json kwargs>)",
        note="ALWAYS use json.dumps(obj.model_dump(mode='json', include/exclude/by_alias/exclude_*=...), "
             "sort_keys/indent/separators/...=...) and `import json`. v1 .json() was json.dumps-based and wrote "
             "'{\"a\": 1}' (', ' and ': ' separators); model_dump_json() writes compact '{\"a\":1}' and rejects "
             "json.dumps kwargs, so it silently changes printed/stored/hashed output. Use model_dump_json() only if "
             "the exact JSON text provably does not matter.",
        before="obj.json(exclude_none=True)", after="json.dumps(obj.model_dump(mode='json', exclude_none=True))"),
    ".parse_obj()": _method("parse_obj", "model_validate"),
    ".parse_raw()": _method("parse_raw", "model_validate_json"),
    ".parse_file()": _method("parse_file", "model_validate_json",
                             action="replace .parse_file(path) with .model_validate_json(Path(path).read_text())",
                             after="Model.model_validate_json(Path(path).read_text())"),
    ".from_orm()": _method("from_orm", "model_validate",
                           action="replace .from_orm(obj) with .model_validate(obj) and set from_attributes=True in model_config",
                           note="Requires model_config = ConfigDict(from_attributes=True) (was orm_mode=True)."),
    ".schema()": _method("schema", "model_json_schema",
                         action="replace .schema() with .model_json_schema(ref_template=\"#/definitions/{model}\")",
                         note="v2 writes nested-model refs as '#/$defs/Name' (v1: '#/definitions/Name') and stores them "
                              "under the '$defs' key. Pass ref_template=\"#/definitions/{model}\" to keep v1 ref strings, "
                              "so schema-derived output (prompts, snapshots, golden hashes) stays identical; code reading "
                              "s['definitions'] must also accept s['$defs'].",
                         after='Model.model_json_schema(ref_template="#/definitions/{model}")',
                         anchor="changes-to-json-schema-generation"),
    ".schema_json()": _method("schema_json", "model_json_schema",
                              action="replace .schema_json() with json.dumps(Model.model_json_schema())",
                              after="json.dumps(Model.model_json_schema())"),
    ".construct()": _method("construct", "model_construct"),
    ".copy()": _method("copy", "model_copy",
                       note="model_copy supports update= and deep=; include/exclude are gone -> "
                            "model_validate(obj.model_dump(include=...))."),
    ".update_forward_refs()": _method("update_forward_refs", "model_rebuild"),
    ".validate()": _method("validate", "model_validate"),
    "__fields__": KBEntry("model_methods", "model_fields", "replace __fields__ with model_fields",
                          ("__fields__",), ("model_fields",), "changes-to-pydanticbasemodel",
                          "model_fields values are FieldInfo (no ModelField); use .is_required(), .annotation.",
                          "Model.__fields__", "Model.model_fields"),
    "__fields_set__": KBEntry("model_methods", "model_fields_set", "replace __fields_set__ with model_fields_set",
                              ("__fields_set__",), ("model_fields_set",), "changes-to-pydanticbasemodel"),
    "__config__": KBEntry("model_methods", "model_config", "replace __config__ with model_config (a dict)",
                          ("__config__",), ("model_config",), "changes-to-config"),
    # ---- Field kwargs ----------------------------------------------------
    "Field(regex=...)": KBEntry("field", "Field(pattern=...)", "rename Field(regex=...) to Field(pattern=...)",
                                ("regex",), ("pattern",), "changes-to-pydanticfield",
                                "v2 uses the Rust regex engine (no look-arounds/backrefs); plain patterns are unchanged.",
                                'kb_id: str = Field(..., regex=r"^KB-\\d{2}$")',
                                'kb_id: str = Field(..., pattern=r"^KB-\\d{2}$")'),
    "Field(min_items=...)": KBEntry("field", "Field(min_length=...)", "rename Field(min_items=...) to Field(min_length=...)",
                                    ("min_items",), ("min_length",), "changes-to-pydanticfield", "",
                                    "items: list[int] = Field(..., min_items=1)", "items: list[int] = Field(..., min_length=1)"),
    "Field(max_items=...)": KBEntry("field", "Field(max_length=...)", "rename Field(max_items=...) to Field(max_length=...)",
                                    ("max_items",), ("max_length",), "changes-to-pydanticfield"),
    "Field(unique_items=...)": KBEntry("field", None, "remove unique_items; use a set/frozenset type or a validator",
                                       ("unique_items",), ("set[",), "changes-to-pydanticfield"),
    "Field(allow_mutation=...)": KBEntry("field", "Field(frozen=...)", "replace allow_mutation=False with frozen=True",
                                         ("allow_mutation",), ("frozen",), "changes-to-pydanticfield"),
    "Field(const=...)": KBEntry("field", "Literal[...]", "replace const=True with a Literal[...] annotation",
                                ("const",), ("Literal",), "changes-to-pydanticfield"),
    "Field(env=...)": KBEntry("settings", "validation_alias", "replace Field(env=...) with Field(validation_alias=...) "
                              "on a pydantic-settings BaseSettings", ("env=",), ("validation_alias",),
                              "basesettings-has-moved-to-pydantic-settings"),
    # ---- moved / removed --------------------------------------------------
    "BaseSettings": KBEntry("settings", "pydantic_settings.BaseSettings",
                            "import BaseSettings from pydantic_settings, add pydantic-settings to requirements, "
                            "and use model_config = SettingsConfigDict(...)",
                            ("BaseSettings",), ("pydantic_settings", "pydantic-settings"),
                            "basesettings-has-moved-to-pydantic-settings", "",
                            "from pydantic import BaseSettings", "from pydantic_settings import BaseSettings"),
    "parse_obj_as": KBEntry("functions", "TypeAdapter.validate_python",
                            "replace parse_obj_as(T, data) with TypeAdapter(T).validate_python(data)",
                            ("parse_obj_as",), ("TypeAdapter",), "introduction-of-typeadapter", "",
                            "parse_obj_as(list[int], data)", "TypeAdapter(list[int]).validate_python(data)"),
    "parse_raw_as": KBEntry("functions", "TypeAdapter.validate_json",
                            "replace parse_raw_as(T, raw) with TypeAdapter(T).validate_json(raw)",
                            ("parse_raw_as",), ("TypeAdapter",), "introduction-of-typeadapter"),
    "schema_of": KBEntry("functions", "TypeAdapter.json_schema",
                         "replace schema_of(T) with TypeAdapter(T).json_schema()",
                         ("schema_of",), ("TypeAdapter",), "introduction-of-typeadapter"),
    "pydantic.generics": KBEntry("generics", "BaseModel + typing.Generic",
                                 "subclass BaseModel and Generic[T] directly; drop pydantic.generics.GenericModel",
                                 ("GenericModel", "pydantic.generics"), ("Generic[",),
                                 "changes-to-pydanticgenericsgenericmodel", "",
                                 "class Resp(GenericModel, Generic[T]): ...", "class Resp(BaseModel, Generic[T]): ..."),
    "constr(regex=...)": KBEntry("types", "constr(pattern=...)",
                                 "rename constr(regex=...) to constr(pattern=...) "
                                 "(or Annotated[str, StringConstraints(pattern=...)])",
                                 ("regex",), ("pattern", "StringConstraints"), "constrained-types"),
    "conlist(min_items=...)": KBEntry("types", "conlist(min_length=...)", "rename conlist(min_items=) to min_length=",
                                      ("min_items",), ("min_length",), "constrained-types"),
}

# v1 config keys (slicer flags each key line inside `class Config`)
_CONFIG_RENAMES = {
    "allow_population_by_field_name": "populate_by_name", "anystr_lower": "str_to_lower",
    "anystr_strip_whitespace": "str_strip_whitespace", "anystr_upper": "str_to_upper",
    "keep_untouched": "ignored_types", "max_anystr_length": "str_max_length",
    "min_anystr_length": "str_min_length", "orm_mode": "from_attributes",
    "schema_extra": "json_schema_extra", "validate_all": "validate_default",
}
_CONFIG_REMOVED = {
    "allow_mutation": "use frozen=True (inverse meaning)", "error_msg_templates": "removed",
    "fields": "use Annotated[..., Field(...)] on the fields", "getter_dict": "removed with orm_mode",
    "smart_union": "default union_mode is already 'smart'", "underscore_attrs_are_private": "always on in v2",
    "json_loads": "removed", "json_dumps": "removed", "copy_on_model_validation": "removed",
    "post_init_call": "removed", "json_encoders": "deprecated; prefer @field_serializer",
}
for _k, _v in _CONFIG_RENAMES.items():
    SYMBOL_KB[_k] = KBEntry("config", _v, f"rename config key `{_k}` to `{_v}` inside ConfigDict(...)",
                            (_k,), (_v,), "changes-to-config", "", f"{_k} = True", f"{_v}=True")
for _k, _v in _CONFIG_REMOVED.items():
    SYMBOL_KB[_k] = KBEntry("config", None, f"drop config key `{_k}` ({_v})", (_k,), (), "changes-to-config", _v)


def kb_entry(symbol: str) -> KBEntry:
    """KB entry for a Stage-2 symbol (generic entry for anything unknown)."""
    if symbol in SYMBOL_KB:
        return SYMBOL_KB[symbol]
    m = re.match(r"^(\w+)\((\w+)=\.\.\.\)$", symbol)            # constr(curtail_length=...) etc.
    if m and f"Field({m[2]}=...)" in SYMBOL_KB:
        base = SYMBOL_KB[f"Field({m[2]}=...)"]
        return KBEntry("types", base.v2, base.action.replace("Field", m[1]), base.match, base.v2_match, "constrained-types")
    if symbol.startswith("pydantic."):
        return KBEntry("imports", None, f"replace the import from `{symbol}` with its v2 location",
                       (symbol,), (), "moved-in-pydantic-v2")
    if symbol.startswith("Constrained") or symbol in ("PyObject", "NoneStr", "StrBytes", "stricturl"):
        return KBEntry("imports", None, f"`{symbol}` was removed in v2; replace with the documented alternative",
                       (symbol,), (), "removed-in-pydantic-v2")
    term = symbol.strip("@.()")
    return KBEntry("other", None, f"migrate `{symbol}` to its Pydantic v2 equivalent", (term,), ())


# --------------------------------------------------------------------------- #
# Tavily client (httpx, disk cache)
# --------------------------------------------------------------------------- #
class TavilyError(RuntimeError):
    pass


def tavily_search(query: str, *, api_key: str, use_cache: bool = True, max_results: int = 3,
                  timeout: float = 30.0) -> tuple[dict, bool]:
    """Returns (response, from_cache)."""
    body = {
        "query": query, "search_depth": "advanced", "chunks_per_source": 3, "max_results": max_results,
        "include_domains": [DOC_DOMAIN], "include_raw_content": "markdown", "include_answer": False,
    }
    key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    cache_file = CACHE_DIR / f"{key}.json"
    if use_cache and cache_file.is_file():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8")), True
        except (OSError, json.JSONDecodeError):
            pass
    last = ""
    for attempt in range(3):
        try:
            r = httpx.post(TAVILY_URL, json=body, timeout=timeout,
                           headers={"Authorization": f"Bearer {api_key}"})
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if r.status_code == 200:
                data = r.json()
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(data), encoding="utf-8")
                return data, False
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
    raise TavilyError(last)


# --------------------------------------------------------------------------- #
# Markdown mining
# --------------------------------------------------------------------------- #
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass
class _Section:
    heading: str
    url: str
    text_lines: list[str] = field(default_factory=list)
    code: list[str] = field(default_factory=list)
    snippet: bool = False          # built from Tavily `content` chunks (flattened): text evidence only


_SKIP_URL = re.compile(r"/blog/|pydantic\.dev/(?:docs/validation/)?1\.\d+")
_VERSION_SEG = re.compile(r"/(?:docs/validation/)?(?:latest|dev|\d+\.\d+(?:\.\d+)?)(?=/)")


def page_key(url: str) -> str:
    """Same page regardless of anchor or doc version: .../2.10/migration#x == .../latest/migration."""
    u = url.split("#", 1)[0].split("?", 1)[0].lower().rstrip("/")
    return _VERSION_SEG.sub("", u)


def _is_nav(line: str) -> bool:
    return "[x]" in line or line.count(" * ") >= 4 or line.startswith(("Title:", "URL Source:", "Skip to content"))


def split_sections(markdown: str, url: str, snippet: bool = False) -> list[_Section]:
    sections = [_Section("", url, snippet=snippet)]
    in_code, buf = False, []
    for line in markdown.splitlines():
        if _FENCE.match(line):
            if in_code:
                sections[-1].code.append("\n".join(buf).strip())
                buf = []
            in_code = not in_code
            continue
        if in_code:
            buf.append(line)
            continue
        h = _HEADING.match(line)
        if h:
            sections.append(_Section(h[2].strip(), url, snippet=snippet))
        else:
            sections[-1].text_lines.append(line)
    if in_code and buf:
        sections[-1].code.append("\n".join(buf).strip())
    return [s for s in sections if s.text_lines or s.code]


TOPIC_HEADINGS = {
    "validators": ("validator",), "config": ("config",), "model_methods": ("BaseModel",),
    "field": ("Field",), "root": ("RootModel", "root"), "settings": ("BaseSettings",),
    "functions": ("TypeAdapter",), "generics": ("GenericModel",), "types": ("Constrained", "constr"),
    "imports": ("Moved", "Removed"),
}
_RX_CACHE: dict[str, re.Pattern] = {}


def _plain(s: str) -> str:
    return s.replace("`", "").replace("\\_", "_")


def _term_rx(term: str) -> re.Pattern:
    """Whole-token match: `schema()` must not match `json_schema()`."""
    if term not in _RX_CACHE:
        pre = r"(?<![\w.])" if term[:1].isalnum() or term[:1] == "_" else ""
        post = r"(?!\w)" if term[-1:].isalnum() or term[-1:] == "_" else ""
        _RX_CACHE[term] = re.compile(pre + re.escape(term) + post)
    return _RX_CACHE[term]


def _hits(text: str, terms: tuple[str, ...]) -> int:
    t = _plain(text)
    return sum(len(_term_rx(term).findall(t)) for term in terms if term)


def _is_python(code: str) -> bool:
    """Doc examples must parse; flattened fragments ('import X adapter = ...') don't."""
    import ast
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def mine_symbol(entry: KBEntry, sections: list[_Section]) -> dict[str, Any]:
    """Best evidence lines and before/after code blocks for one symbol."""
    heads = TOPIC_HEADINGS.get(entry.topic, ())
    scored = []
    for s in sections:
        body = "\n".join(s.text_lines)
        v1_body = _hits(body, entry.match)
        if not v1_body and not any(_hits(c, entry.match) for c in s.code):
            continue                                     # section never names the v1 symbol
        score = 3 * _hits(s.heading, entry.match + entry.v2_match) + 2 * v1_body \
            + _hits(body, entry.v2_match) + sum(_hits(c, entry.match + entry.v2_match) for c in s.code) \
            + (4 if any(h.lower() in s.heading.lower() for h in heads) else 0)
        scored.append((score, s))
    scored.sort(key=lambda x: -x[0])

    # evidence: lines naming the v1 symbol, those also naming the v2 symbol first
    cand = []
    for rank, (_, s) in enumerate(scored[:4]):
        for line in s.text_lines:
            ln = line.strip()
            if ln and not _is_nav(ln) and _hits(ln, entry.match):
                both = bool(entry.v2_match) and _hits(ln, entry.v2_match) > 0
                cand.append((not both, rank, ln, s))
    cand.sort(key=lambda c: (c[0], c[1]))
    evidence, before, after, seen = [], [], [], set()
    for _, _, ln, s in cand:
        key = _plain(ln)
        if key not in seen and len(evidence) < 4:
            seen.add(key)
            evidence.append({"text": ln[:400], "url": s.url, "section": s.heading})

    for _, s in scored[:3]:
        if s.snippet:
            continue
        for code in s.code:
            snippet = code[:MAX_CODE_CHARS]
            code_lines = snippet.splitlines()
            if not snippet or snippet in seen or max(map(len, code_lines)) > 160 \
                    or (len(code_lines) == 1 and len(snippet) > 100) or not _is_python(snippet):
                continue                       # empty, duplicate, or flattened onto one line
            has_v2, has_v1 = _hits(code, entry.v2_match), _hits(code, entry.match)
            if has_v2 and len(after) < 2:
                seen.add(snippet)
                after.append({"code": snippet, "url": s.url, "section": s.heading})
            elif has_v1 and not has_v2 and len(before) < 1:
                seen.add(snippet)
                before.append({"code": snippet, "url": s.url, "section": s.heading})
    return {"evidence": evidence, "before": before, "after": after}


# --------------------------------------------------------------------------- #
# Query building
# --------------------------------------------------------------------------- #
def _runtime_errors(stage2: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for info in stage2.get("details", {}).get("files", {}).values():
        for c in info.get("crashes", []):
            msg = next((ln.strip() for ln in (c.get("message") or "").splitlines() if ln.strip()),
                       c.get("exc_type") or "")[:200]
            for s in c.get("symbols", []):
                if msg and msg not in out.setdefault(s, []):
                    out[s].append(msg)
    return out


def build_query(topic: str, symbols: list[str], runtime: dict[str, list[str]]) -> str:
    pairs = []
    for s in symbols:
        e = kb_entry(s)
        pairs.append(f'"{s}" -> "{e.v2}"' if e.v2 else f'"{s}" removed')
    q = f"Pydantic V2 migration guide {TOPIC_TITLES.get(topic, topic)}: " + "; ".join(pairs)
    errs = [m for s in symbols for m in runtime.get(s, [])]
    if errs:
        q += f'. Error: "{errs[0][:120]}"'
    q += ". Before and after code example."
    if len(q) > MAX_QUERY_CHARS:   # keep the most important symbols, drop the tail
        q = q[: MAX_QUERY_CHARS - 3].rsplit(";", 1)[0] + "..."
    return q


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def ground(stage2: dict, *, max_queries: int = 6, use_cache: bool = True,
           api_key: str | None = None) -> dict[str, Any]:
    symbols: list[str] = list(dict.fromkeys(stage2.get("breakage_symbols", [])))
    runtime = _runtime_errors(stage2)
    target = stage2.get("details", {}).get("pydantic_version")

    groups: dict[str, list[str]] = {}
    for s in symbols:                       # stage-2 order = priority order
        groups.setdefault(kb_entry(s).topic, []).append(s)

    key = api_key or env_key("tavily_key", "TAVILY_API_KEY")
    queries, sources = [], []
    pages: dict[str, dict] = {}            # page_key -> {"url", "md", "raw"}; best copy of each doc page
    for i, (topic, syms) in enumerate(groups.items()):
        q = build_query(topic, syms, runtime)
        rec: dict[str, Any] = {"topic": topic, "symbols": syms, "query": q}
        if not key:
            rec["status"] = "skipped: no Tavily key (tavily_key / TAVILY_API_KEY)"
        elif i >= max_queries:
            rec["status"] = f"skipped: max_queries={max_queries}"
        else:
            try:
                data, cached = tavily_search(q, api_key=key, use_cache=use_cache)
                used = skipped = 0
                for r in data.get("results", []):
                    url = r.get("url", "")
                    if not url or _SKIP_URL.search(url):
                        skipped += 1
                        continue
                    raw = r.get("raw_content") or ""
                    pk, prev = page_key(url), pages.get(page_key(url))
                    if prev is None or (raw and not prev["raw"]):
                        pages[pk] = {"url": url, "md": raw or r.get("content") or "", "raw": bool(raw)}
                        used += 1
                rec.update(status="ok", cached=cached, results=len(data.get("results", [])),
                           new_pages=used, skipped_pages=skipped)
            except TavilyError as e:
                rec["status"] = f"error: {e}"
        queries.append(rec)

    pool: list[_Section] = []              # every symbol is mined against all retrieved pages
    for p in pages.values():
        pool += split_sections(p["md"], p["url"], snippet=not p["raw"])
        sources.append(p["url"])

    out_syms: dict[str, Any] = {}
    for s in symbols:
        e = kb_entry(s)
        mined = mine_symbol(e, pool)
        grounded = bool(mined["evidence"] or mined["after"])
        out_syms[s] = {
            "topic": e.topic, "v2": e.v2, "action": e.action, "guidance": e.note, "doc_url": e.doc_url,
            "runtime_errors": runtime.get(s, []),
            "evidence": mined["evidence"],
            "examples": {
                "before": mined["before"] or ([{"code": e.before, "url": e.doc_url, "section": "reference"}] if e.before else []),
                "after": mined["after"] or ([{"code": e.after, "url": e.doc_url, "section": "reference"}] if e.after else []),
            },
            "grounded": grounded,                       # True = backed by live doc evidence
            "source": "tavily" if grounded else "builtin",
        }

    ok = [q for q in queries if q.get("status") == "ok"]
    return {
        "provider": "tavily",
        "pydantic_target": target,
        "queries": queries,
        "symbols": out_syms,
        "sources": sources,
        "stats": {
            "symbols": len(symbols),
            "grounded": sum(v["grounded"] for v in out_syms.values()),
            "queries_ok": len(ok),
            "pages": len(pages), "pages_with_markdown": sum(p["raw"] for p in pages.values()),
            "credits_est": 2 * sum(1 for q in ok if not q.get("cached")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 3: ground Stage-2 symbols in the Pydantic v2 docs")
    ap.add_argument("stage2_json", help="file holding the Stage-2 payload (or the full endpoint response)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--max-queries", type=int, default=6)
    a = ap.parse_args(argv)
    data = json.loads(Path(a.stage2_json).read_text(encoding="utf-8"))
    print(json.dumps(ground(data.get("stage2", data), max_queries=a.max_queries, use_cache=not a.no_cache), indent=2))
    return 0


if __name__ == "__main__":   # run from the Reforge root: python -m src.backend.planner.grounder stage2.json
    raise SystemExit(main())
