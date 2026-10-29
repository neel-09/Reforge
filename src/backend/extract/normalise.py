import ast, re, textwrap, httpx
from typing import Optional

GITHUB_REPO_REGEX = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$")
RESERVED = {"settings", "pricing", "orgs", "topics", "login", "explore", "marketplace"}

def normalize_github_url(url: str) -> Optional[str]:
    match = GITHUB_REPO_REGEX.match(url.strip())
    if not match:
        return None

    owner, repo = match.group(1), match.group(2)
    if owner.lower() in RESERVED:
        return None
    clean_repo_url = f"https://github.com/{owner}/{repo}"

    try:
        # stream("GET") downloads only headers and automatically closes the socket on exit
        with httpx.stream("GET", clean_repo_url, timeout=5.0, follow_redirects=True) as response:
            if response.status_code == 200:
                return f"{clean_repo_url}.git"
    except httpx.RequestError:
        pass
        
    return None

def normalize_pasted_code(raw_text: str) -> str:
    """Turn pasted input into a Python module.

    * Markdown with ``` fences -> the code inside the fences (prose around them is dropped).
    * Input that already parses as Python -> left untouched (keeps comments, **kwargs, backticks in strings).
    * Anything else -> best-effort markdown cleanup.
    """
    text = textwrap.dedent(raw_text.replace("\r\n", "\n")).strip("\n")
    fences = re.findall(r"^\s*```[A-Za-z0-9_+-]*[^\n]*\n(.*?)^\s*```\s*$", text, flags=re.S | re.M)
    if fences:
        text = "\n\n".join(textwrap.dedent(f).strip("\n") for f in fences)
    elif not _parses(text):
        text = text.strip()
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = re.sub(r"`([^`\n\r]+)`", r"\1", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"^\s*#+\s+.*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n\s*\n\s*\n", "\n\n", text).strip()

    # Auto-prepend missing Pydantic imports if BaseModel subclass is present
    if "BaseModel" in text and "from pydantic" not in text and "import pydantic" not in text:
        text = "from typing import List, Optional, Dict, Any\nfrom pydantic import BaseModel, Field, validator, root_validator\n\n" + text

    return text + "\n"


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False
