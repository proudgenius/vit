"""MCP server for vit's audit snapshots: `vit mcp -C <project>`.

Speaks MCP's JSON-RPC over stdin and stdout, one message per line, using only
the standard library. It gives an assistant five tools: record a timeline
(`snapshot`) and read the record back cheaply (`summary`, `history`, `diff`,
`timelines`).

It only reads from Resolve. The one thing it writes is the vit project (a git
repo of JSON); it never changes or restores a timeline.
"""

import json
import sys
from typing import Optional

from . import __version__
from .core import GitError
from .snapshot import (
    SnapshotError,
    cap_diff,
    diff_counts,
    diff_timeline,
    ensure_project,
    find_timeline,
    read_index,
    resolve_key,
    snapshot,
    summarize_timeline,
    timeline_log,
)

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = """\
Vit keeps a record of the edit: what each timeline looks like and how it changed. \
It only reads from DaVinci Resolve; it never changes or restores a timeline.

Start of a session: call `snapshot` with message "session start". If the editor \
changed the timeline by hand since last time, the result says exactly what. Then \
`history` for what was done before and `summary` for what the cut is now. Read \
these instead of scripting Resolve to list the timeline.

Around every change you make to a timeline (with RavenSeek's make_timeline, a \
Resolve script, or anything else): `snapshot` before, make the change, `snapshot` \
after with a message saying what and why, and with `journal` set to the call you \
made. Check `domains_changed` and `diff` against what you meant to do. If they \
don't match, stop and tell the user.

When you build a timeline from a script or an edit list, pass `links` so each \
clip keeps its scene and line: every entry needs `path` plus `frames` {in, out} \
or `start_seconds`/`end_seconds`, and a short `ref` such as "scene 2 line 4".

Not recorded: effects, transitions, audio fades, keyframes, speed ramps, clip \
markers, Media Pool changes, project settings, renders. This is a record, not a \
rollback: to be able to go back, duplicate the timeline in Resolve first.\
"""

_TIMELINE_ARG = {
    "type": "string",
    "description": "Timeline name or key. Leave out if only one timeline has snapshots.",
}

TOOLS = [
    {
        "name": "snapshot",
        "title": "Record a timeline",
        "description": (
            "Record a timeline's current state from Resolve and commit it. Returns "
            "the commit, which domains changed since the last snapshot (cuts, color, "
            "audio, markers, metadata) with counts (+added -removed ~changed), and "
            "the first lines of the diff. Reads "
            "from Resolve; changes nothing there."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "What was done and why, in one line."},
                "timeline": {"type": "string",
                             "description": "Timeline name. Default: the one open in Resolve."},
                "journal": {"description": "What was asked for: the tool call or script. "
                                           "Stored with the snapshot; not read back."},
                "links": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ties clips to outside references. Each entry: `path`, "
                                   "`frames` {in, out} or `start_seconds`/`end_seconds`, "
                                   "a short `ref`, and anything else worth keeping.",
                },
                "color": {"type": "boolean",
                          "description": "Also export a LUT/DRX per clip. Switches Resolve "
                                         "to the Color page; ask the user first."},
                "max_lines": {"type": "integer",
                              "description": "Diff lines to return (default 20; 0 for all)."},
            },
            "required": ["message"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "summary",
        "title": "What the cut is",
        "description": (
            "One line per clip for a recorded timeline (track, timecode in–out, "
            "name, source frames, link ref), plus markers. Reads the last snapshot, "
            "not Resolve; snapshot first if the timeline may have changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeline": _TIMELINE_ARG,
                "ref": {"type": "string",
                        "description": "A commit hash from `history`, to see the cut as it was then."},
                "audio": {"type": "boolean", "description": "List audio clips too."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "history",
        "title": "What was done",
        "description": "One line per recorded change to a timeline, newest first: "
                       "commit, date, domains touched, message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeline": _TIMELINE_ARG,
                "count": {"type": "integer", "description": "Entries to return (default 20)."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "diff",
        "title": "What changed since a commit",
        "description": "Changes between a commit and the last snapshot of a timeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeline": _TIMELINE_ARG,
                "ref": {"type": "string",
                        "description": "Commit to compare from (from `history`)."},
                "max_lines": {"type": "integer",
                              "description": "Diff lines to return (default 40; 0 for all)."},
            },
            "required": ["ref"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "timelines",
        "title": "Recorded timelines",
        "description": "The timelines that have snapshots: name and key.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


_MORE = "set max_lines to 0 for all"


class ToolError(Exception):
    pass


def _key_for(project_dir: str, timeline: Optional[str]) -> str:
    if timeline:
        return resolve_key(project_dir, timeline)
    index = read_index(project_dir)
    if len(index) == 1:
        return next(iter(index))
    if not index:
        raise SnapshotError("No timeline has a snapshot yet. Call snapshot first.")
    names = ", ".join(f"'{v.get('name')}'" for v in index.values())
    raise SnapshotError(f"Several timelines have snapshots; say which: {names}.")


def _strip_header(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.startswith("  Timeline:"))


def _tool_snapshot(project_dir: str, args: dict, connect) -> str:
    resolve, project = connect()
    timeline = find_timeline(project, args.get("timeline"))
    result = snapshot(
        project_dir, timeline, project, args.get("message", ""),
        resolve_app=resolve, color=bool(args.get("color")),
        journal=args.get("journal"), links=args.get("links"),
    )
    # Plain text, not JSON: it is what the assistant reads, so every token counts
    lines = [f"timeline: {result['timeline']}",
             f"commit: {result['commit'] or 'none'}"]
    if result["changed"]:
        counts = diff_counts(result["diff"])
        parts = []
        for domain in result["domains_changed"]:
            c = counts.get(domain, {})
            detail = " ".join(f"{sym}{c[k]}" for sym, k in
                              (("+", "added"), ("-", "removed"), ("~", "changed")) if c.get(k))
            parts.append(f"{domain} ({detail})" if detail else domain)
        lines.append(f"changed: {', '.join(parts)}")
        shown, _, _ = cap_diff(result["diff"], int(args.get("max_lines", 20)), _MORE)
        lines.append(_strip_header(shown))
    else:
        lines.append("changed: nothing")
    if result["links"]:
        l = result["links"]
        line = f"links: {l['matched']} of {l['given']} matched a clip"
        if l["unmatched"]:
            line += f"; no clip for entries {l['unmatched']} (counted from 0)"
        lines.append(line)
    for issue in result["issues"]:
        lines.append(f"issue: {issue}")
    return "\n".join(lines)


def call_tool(project_dir: str, name: str, args: dict, connect) -> str:
    """Run one tool and return its text. Raises ToolError with a plain message."""
    try:
        if name == "snapshot":
            return _tool_snapshot(project_dir, args, connect)
        if name == "summary":
            key = _key_for(project_dir, args.get("timeline"))
            return summarize_timeline(project_dir, key, ref=args.get("ref"),
                                      audio=bool(args.get("audio")))
        if name == "history":
            key = _key_for(project_dir, args.get("timeline"))
            commits = timeline_log(project_dir, key, int(args.get("count") or 20))
            if not commits:
                return "No commits yet."
            return "\n".join(
                f"{c['hash']}  {c['date']}  [{','.join(c['domains'])}]  {c['message']}"
                for c in commits)
        if name == "diff":
            if not args.get("ref"):
                raise ToolError("diff needs a ref: a commit hash from history.")
            key = _key_for(project_dir, args.get("timeline"))
            text, _ = diff_timeline(project_dir, key, args["ref"])
            shown, _, _ = cap_diff(text, int(args.get("max_lines", 40)), _MORE)
            return _strip_header(shown)
        if name == "timelines":
            index = read_index(project_dir)
            if not index:
                return "No timeline has a snapshot yet."
            return "\n".join(f"{v.get('name', '')}  ({k})" for k, v in sorted(
                index.items(), key=lambda kv: kv[1].get("name", "")))
    except ToolError:
        raise
    except (SnapshotError, GitError, OSError, ValueError) as e:
        raise ToolError(str(e))
    except Exception as e:
        # ResolveConnectionError and anything Resolve's API throws
        raise ToolError(f"{type(e).__name__}: {e}")
    raise ToolError(f"There is no tool called '{name}'.")


def handle(project_dir: str, request: dict, connect) -> Optional[dict]:
    """Answer one JSON-RPC message. Returns None for notifications."""
    method = request.get("method")
    req_id = request.get("id")
    if req_id is None:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    if method == "initialize":
        asked = (request.get("params") or {}).get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "vit", "title": "Vit", "version": __version__},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            text = call_tool(project_dir, params.get("name", ""),
                             params.get("arguments") or {}, connect)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except ToolError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def _connect_resolve():
    from .resolve_connect import connect, current_project
    resolve = connect()
    return resolve, current_project(resolve)


def serve(project_dir: str, stdin=None, stdout=None, connect=_connect_resolve) -> None:
    """Run the server until stdin closes."""
    stdin = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    # vit's own code prints progress and warnings; keep them off the protocol stream
    real_stdout, sys.stdout = sys.stdout, sys.stderr
    try:
        ensure_project(project_dir)
        for raw in stdin:
            line = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}}
            else:
                response = handle(project_dir, request, connect) if isinstance(request, dict) else None
            if response is not None:
                out.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                out.flush()
    finally:
        sys.stdout = real_stdout
