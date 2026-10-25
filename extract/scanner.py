import re,httpx, ast, sys, extract_version
from pathlib import Path

module_name = "pydantic"
V1_DECORATORS = {"validator", "root_validator"}
V1_METHODS = {"dict", "json", "parse_obj", "parse_raw", "from_orm", "schema", "schema_json", "copy"}
V1_FIELD_KWARGS = {"regex", "min_items", "max_items", "unique_items", "allow_mutation"}
V1_SYMBOLS = {"constr", "conint", "confloat", "conlist", "conset", "conbytes", "GenericModel", "BaseSettings"}
V2_SYMBOLS = {"field_validator", "model_validator", "ConfigDict", "RootModel", "model_validate", "model_validate_json", "model_dump", "model_dump_json", "model_json_schema"}


def scan_environment(ws_path: Path | str, input_type: str) -> dict:
    ws_path = Path(ws_path)
    migrated = False

    if input_type == "github":
        current_version, companion_names = has_module(module_name, ws_path)
        # Fallback if github repo doesn't have a requirements.txt
        if not current_version:
            companion_names, migrated = has_no_specified_module(ws_path)
            current_version = "1.10.12"
    else:
        companion_names, migrated = has_no_specified_module(ws_path)
        current_version = "1.10.12"

    latest_version = extract_version.version(module_name)
    cve_count, vulns = check_cve(module_name, current_version)
    
    return dict(
        package=module_name,
        current_version=current_version,
        latest_version=latest_version,
        companion_names=companion_names,
        vulnerabilities=vulns,
        cve_count=cve_count,
        migration=migrated)


def has_no_specified_module(repo_dir: Path) -> tuple[list[str], bool]:
    has_pydantic = False
    has_v2 = False
    detected_v1 = set()
    detected_deps = set()
    stdlib = sys.stdlib_module_names

    # Iterate over all python files in the workspace directory instead of checking a single file
    for py_file in repo_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            # 1. Imports & Companion Detection
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                names = [alias.name for alias in node.names]
                root_pkg = (mod or (names[0] if names else "")).split(".")[0]

                if root_pkg and root_pkg not in stdlib:
                    detected_deps.add(root_pkg)
                if module_name in mod or any(module_name in n for n in names):
                    has_pydantic = True
                if any(sym in V2_SYMBOLS for sym in names):
                    has_v2 = True
                for sym in names:
                    if sym in V1_SYMBOLS or sym in V1_DECORATORS:
                        detected_v1.add(sym)

            # 2. Legacy `class Config`
            elif isinstance(node, ast.ClassDef) and node.name == "Config":
                detected_v1.add("class Config")

            # 3. Legacy `__root__` custom root model
            elif isinstance(node, (ast.AnnAssign, ast.Assign)):
                target_ids = [t.id for t in (node.targets if hasattr(node, "targets") else [node.target]) if isinstance(t, ast.Name)]
                if "__root__" in target_ids:
                    detected_v1.add("__root__")

            # 4. Legacy Decorators
            elif isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    dec_name = dec.func.id if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) else getattr(dec, "id", None)
                    if dec_name in V1_DECORATORS:
                        detected_v1.add(f"@{dec_name}")

            # 5. Legacy Methods
            elif isinstance(node, ast.Attribute) and node.attr in V1_METHODS:
                detected_v1.add(f".{node.attr}()")

            # 6. Legacy Field kwargs
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in V1_FIELD_KWARGS:
                        detected_v1.add(f"Field({kw.arg}=)")

    companion_names = sorted([pkg for pkg in detected_deps if pkg.lower() != module_name])
    needs_migration = bool(detected_v1) or (has_pydantic and not has_v2)

    # Automatically generate requirements.txt so harness.py can run isolated
    manifest_lines = [f"{module_name}==1.10.12"] + companion_names
    (repo_dir / "requirements.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    return companion_names, needs_migration


def has_module(target_module: str, repo_dir: Path) -> tuple[str | None, list[str]]:
    manifest = repo_dir / "requirements.txt"
    if not manifest.is_file():
        return None, []

    pattern = re.compile(rf"^{re.escape(target_module)}(\s*[<>=!~]|$)")
    companion_name = []
    current_version = None

    with open(manifest, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lower()
            if not line or line.startswith(("#", "-")):
                continue
            
            if pattern.match(line):
                current_version = line.split("==")[1].strip() if "==" in line else None
            else:
                companion_name.append(line)
                
    return current_version, companion_name


def check_cve(target_module: str, current_version: str) -> tuple[int, list[dict]]:
    url = "https://api.osv.dev/v1/query"
    payload = {"version": current_version, "package": {"name": target_module, "ecosystem": "PyPI"}}

    try:
        response = httpx.post(url, json=payload, timeout=5.0)
        data = response.json() if response.status_code == 200 else {}
    except httpx.RequestError:
        data = {}

    vulns = data.get("vulns", [])
    vulnerabilities = [{"id": vuln.get("id"), "details": vuln.get("details")} for vuln in vulns]

    return len(vulnerabilities), vulnerabilities  # Safely returns (0, []) if no CVEs found
