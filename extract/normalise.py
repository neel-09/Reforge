import re, requests, os, base64
from openai import OpenAI

def valid_url(url: str) -> str | None:

    # Extracts ONLY the <owner> and <repo> part, stripping /blob/main/... if the user pasted a file URL
    GITHUB_REPO_REGEX = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$")
    RESERVED = {"settings", "pricing", "orgs", "topics", "login", "explore", "marketplace"}
        
    match = GITHUB_REPO_REGEX.match(url.strip())
    if not match:
        return None 

    owner, repo = match.group(1), match.group(2)
    if owner.lower() in RESERVED:
        return None

    clean_git_url = f"https://github.com/{owner}/{repo}.git"
    git_handshake_url = f"{clean_git_url}/info/refs?service=git-upload-pack"

    try:
        with requests.get(git_handshake_url, timeout=5, stream=True) as r:
            if r.status_code == 200:
                return clean_git_url
            return None
    except requests.RequestException:
        return None

def text_normalise():

    text = """
# My Python Script

```python
def main():
    # **Important**: This prints a message
    print("Hello World")
```
"""
    # 1. Remove code block syntax (```python and ```)
    text = re.sub(r'```[a-z]*\n', '', text)
    text = re.sub(r'```', '', text)
    # 2. Remove inline code backticks (`code` -> code) cleanly
    text = re.sub(r'`([^`\n\r]+)`', r'\1', text)
    # 3. REMOVE Markdown headers entirely (lines starting with #)
    text = re.sub(r'^#+\s+.*\$', '', text, flags=re.MULTILINE)
    # 4. Remove bold/italic syntax (**bold** or *italic*)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    # 5. Remove remaining Python comments (lines or trailing parts starting with #)
    text = re.sub(r'#.*\$', '', text, flags=re.MULTILINE)
    # 6. Clean up leftover blank lines
    text = re.sub(r'\n\s*\n', '\n', text)

    # 7. Save to a .py file
    with open("temp_files/cleaned_script.py", "w", encoding="utf-8") as file:
        file.write(text.strip())

def image_normalise():

    image_path = "image.png" 
    with open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode("utf-8")

    client = OpenAI(base_url="https://api.tokenfactory.us-north1.nebius.com/v1/",api_key=os.environ.get("nebius_token_factory"))
    response = client.chat.completions.create(
        model="nvidia/Nemotron-3.5-Super-VL",
        messages=[
        {
            "role": "system",
            "content": "You are a precise code extractor. Extract code from image. Don't add any comments or markdowns."},
        {
            "role": "user",
            "content": [
                {
                    "type": "text", 
                    "text": "Extract everything from the image."},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64_image}"}}]}])

    # 7. Save to a .py file
    with open("temp_files/cleaned_script-1.py", "w", encoding="utf-8") as file:
        file.write(response.choices[0].message.content.strip())

# --- Test ---
# 1. valid_url normalisation
test_urls = [ "https://github.com/neel-09/legacy-system-repo",
              "https://github.com/neel-09/legacy-system-repo/blob/main/src/schema.py",
              "https://github.com/neel-09",]

for u in test_urls:
    print(f"Input: {u}\n -> Clone URL: {valid_url(u)}\n")

# 2. code snippet normalisation
text_normalise()
print("Text normalisation done!")

# 3. image normalisation
image_normalise()
print("Image normalisation done!")
