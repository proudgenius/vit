"""Build the MCP client config (.mcp.json) for the editing-assistant setup.

Four servers work together:

  davinci-resolve  Blackmagic's ResolveMCP: changes timelines, renders
  ravenseek        finds footage and puts it on a timeline
  ravenwriter      holds the story and the paper edit
  vit              records what changed on each timeline (this package)
"""

import json
import os
import shutil
import sys
from typing import Dict, List, Optional, Tuple

RAVENWRITER_PLACEHOLDER = "<path to RavenWriter: RavenWriter > Settings > AI > Claude > Copy settings>"
RAVENSEEK_PLACEHOLDER = "<path to RavenSeek>"
PYTHON_PLACEHOLDER = "<path to vit's python: run 'vit setup' to fill this in>"
PROJECT_PLACEHOLDER = "<path to your vit record folder>"

# Install locations as each app documents them
_APP_PATHS = {
    "windows": {
        "davinci-resolve": r"C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolveMCP.exe",
        "ravenseek": r"C:\Program Files\RavenSeek\ravenseek.exe",
    },
    "macos": {
        "davinci-resolve": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP",
        "ravenseek": "/Applications/RavenSeek.app/Contents/MacOS/RavenSeek",
    },
    "linux": {
        "davinci-resolve": "/opt/resolve/bin/ResolveMCP",
        "ravenseek": RAVENSEEK_PLACEHOLDER,
    },
}


def current_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def build(platform: Optional[str] = None, python: Optional[str] = None,
          project_dir: Optional[str] = None, only_vit: bool = False) -> dict:
    """Return the .mcp.json content. Missing python/project_dir become placeholders."""
    platform = platform or current_platform()
    paths = _APP_PATHS[platform]
    servers: Dict[str, dict] = {}
    if not only_vit:
        servers["davinci-resolve"] = {
            "type": "stdio", "command": paths["davinci-resolve"], "args": []}
        servers["ravenseek"] = {
            "type": "stdio", "command": paths["ravenseek"], "args": ["--mcp"]}
        servers["ravenwriter"] = {
            "type": "stdio", "command": RAVENWRITER_PLACEHOLDER, "args": ["--mcp"]}
    servers["vit"] = {
        "type": "stdio",
        "command": python or PYTHON_PLACEHOLDER,
        "args": ["-m", "vit", "mcp", "-C", project_dir or PROJECT_PLACEHOLDER],
    }
    return {"mcpServers": servers}


def to_json(config: dict) -> str:
    return json.dumps(config, indent=2) + "\n"


def merge_into(path: str, config: dict) -> Tuple[List[str], List[str], Optional[str]]:
    """Add the servers in `config` to the MCP config file at `path`.

    The vit entry is always written. Entries for the other apps are only added
    when the file has none, so whatever their own setup wrote is kept. The file
    is backed up first, once, as `<path>.before-vit`.

    Returns (names written, names left as they were, backup path or None).
    """
    existing: dict = {}
    backup = None
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
        if not isinstance(existing, dict):
            raise ValueError(f"{path} doesn't hold a JSON object.")
        backup = path + ".before-vit"
        if os.path.exists(backup):
            backup = None
        else:
            shutil.copy2(path, backup)

    servers = existing.setdefault("mcpServers", {})
    written, kept = [], []
    for name, entry in config["mcpServers"].items():
        if name != "vit" and name in servers:
            kept.append(name)
            continue
        servers[name] = entry
        written.append(name)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        f.write(to_json(existing))
    return written, kept, backup


def placeholders_left(config: dict) -> List[str]:
    """Names of servers whose command still needs filling in by hand."""
    return [name for name, entry in config["mcpServers"].items()
            if entry["command"].startswith("<") or any(a.startswith("<") for a in entry["args"])]
