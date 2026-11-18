"""Audit snapshots: a record of what changed on a timeline, taken headlessly.

This is a second way to use vit, alongside the branch/merge workflow:

  - It runs from outside Resolve (CLI, agent, hook), not from the panel.
  - Every timeline gets its own folder, so a project with "rough cut v1",
    "v2" and "v3" keeps a separate history for each instead of overwriting
    one shared `timeline/` folder.
  - Clips are identified by Resolve's unique item ID, so the diff after an
    insert says "one clip added", not "every later clip changed".
  - Source files are referenced by path, not by hashing their contents.
  - Colour is recorded as metadata only unless asked, so a snapshot doesn't
    switch pages or move the playhead under the editor.
  - An optional journal entry (what was asked for: the tool call, the script)
    is committed together with the snapshot (what resulted).

Layout inside the vit project:

    timelines/
      index.json                 # key -> timeline name, project
      <key>/                     # one per timeline; same layout as a vit project
        timeline/cuts.json ...
        assets/manifest.json
    journal/
      20261118T172501Z-tighten-trims.json

Snapshots only ever read from Resolve. Nothing here restores a timeline; for
rollback, duplicate the timeline in Resolve before changing it.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .core import GitError, _run, git_config_get, git_init, git_show_file
from .differ import _frames_to_timecode, format_diff
from .json_writer import _write_json, read_json
from .serializer import serialize_timeline
from .validator import validate_project

TIMELINES_DIR = "timelines"
JOURNAL_DIR = "journal"
INDEX_FILE = "index.json"
LINKS_FILE = "links.json"

# Keys of a link entry that only say *where* the clip is. They are used to
# find the clip on the timeline and are not stored: cuts.json already has them.
_LINK_MATCH_KEYS = {"path", "file", "frames", "fps", "source_tc",
                    "start_seconds", "end_seconds"}

# Domain files inside <key>/, relative to the timeline's own root
DOMAIN_FILES = {
    "cuts": "timeline/cuts.json",
    "color": "timeline/color.json",
    "audio": "timeline/audio.json",
    "effects": "timeline/effects.json",
    "markers": "timeline/markers.json",
    "metadata": "timeline/metadata.json",
}


class SnapshotError(Exception):
    """Raised when a snapshot can't be taken."""
    pass


def _slug(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return slug[:max_len].strip("-") or "untitled"


def timeline_key(timeline) -> str:
    """Folder name for a timeline.

    Resolve's unique ID where available, so renaming a timeline keeps its
    history. Otherwise a slug of the name.
    """
    try:
        uid = timeline.GetUniqueId()
    except (AttributeError, TypeError):
        uid = None
    if uid:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(uid))
    return _slug(timeline.GetName() or "")


def timeline_root(project_dir: str, key: str) -> str:
    return os.path.join(project_dir, TIMELINES_DIR, key)


def find_timeline(project, name: Optional[str] = None):
    """Return the current timeline, or the one called `name`.

    Never changes which timeline is current.
    """
    if not name:
        timeline = project.GetCurrentTimeline()
        if not timeline:
            raise SnapshotError("No timeline is open in Resolve.")
        return timeline

    matches = []
    names = []
    try:
        count = int(project.GetTimelineCount() or 0)
    except (AttributeError, TypeError):
        count = 0
    for idx in range(1, count + 1):
        tl = project.GetTimelineByIndex(idx)
        if not tl:
            continue
        tl_name = tl.GetName() or ""
        names.append(tl_name)
        if tl_name == name:
            matches.append(tl)
    if not matches:
        listing = ", ".join(f"'{n}'" for n in names) or "none"
        raise SnapshotError(f"No timeline called '{name}'. Timelines: {listing}.")
    if len(matches) > 1:
        raise SnapshotError(
            f"{len(matches)} timelines are called '{name}'. Open the one you "
            "mean and snapshot without --timeline."
        )
    return matches[0]


def read_index(project_dir: str) -> dict:
    return read_json(os.path.join(project_dir, TIMELINES_DIR, INDEX_FILE))


def _update_index(project_dir: str, key: str, timeline, project) -> None:
    index = read_index(project_dir)
    entry = {
        "name": timeline.GetName() or "",
        "project": project.GetName() or "",
    }
    if index.get(key) != entry:
        index[key] = entry
        _write_json(os.path.join(project_dir, TIMELINES_DIR, INDEX_FILE), index)


def resolve_key(project_dir: str, name_or_key: str) -> str:
    """Turn a timeline name or key into a key that has snapshots."""
    index = read_index(project_dir)
    if name_or_key in index:
        return name_or_key
    keys = [k for k, v in index.items() if v.get("name") == name_or_key]
    if len(keys) == 1:
        return keys[0]
    if len(keys) > 1:
        raise SnapshotError(
            f"{len(keys)} snapshotted timelines are called '{name_or_key}'. "
            f"Use a key instead: {', '.join(keys)}."
        )
    known = ", ".join(f"'{v.get('name')}'" for v in index.values()) or "none"
    raise SnapshotError(f"No snapshots for '{name_or_key}'. Snapshotted: {known}.")


def load_domains(project_dir: str, key: str, ref: Optional[str] = None) -> Dict[str, dict]:
    """Load a timeline's domain files from disk, or from a git ref."""
    files = {}
    for domain, rel in DOMAIN_FILES.items():
        if ref is None:
            files[domain] = read_json(os.path.join(timeline_root(project_dir, key), rel))
        else:
            # Always forward slashes: this is a git path, not an OS path
            content = git_show_file(project_dir, ref, f"{TIMELINES_DIR}/{key}/{rel}")
            files[domain] = json.loads(content) if content else {}
    return files


def diff_timeline(project_dir: str, key: str, ref: str = "HEAD") -> Tuple[str, List[str]]:
    """Diff a timeline's files on disk against a git ref.

    Returns (human-readable diff, names of the domains that changed).
    """
    old_files = load_domains(project_dir, key, ref)
    new_files = load_domains(project_dir, key)
    changed = [d for d in DOMAIN_FILES if old_files.get(d, {}) != new_files.get(d, {})]
    name = new_files.get("metadata", {}).get("timeline_name", "")
    return format_diff(old_files, new_files, timeline_name=name), changed


def _links_path(project_dir: str, key: str) -> str:
    return os.path.join(timeline_root(project_dir, key), LINKS_FILE)


def read_links(project_dir: str, key: str, ref: Optional[str] = None) -> dict:
    """A timeline's links: clip id -> whatever the caller recorded about it."""
    if ref is None:
        return read_json(_links_path(project_dir, key))
    content = git_show_file(project_dir, ref, f"{TIMELINES_DIR}/{key}/{LINKS_FILE}")
    return json.loads(content) if content else {}


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _entry_range(entry: dict, fps: float) -> Optional[Tuple[float, float]]:
    """A link entry's source range in frames, from `frames` or from seconds."""
    frames = entry.get("frames")
    if isinstance(frames, dict) and "in" in frames and "out" in frames:
        return float(frames["in"]), float(frames["out"])
    if "start_seconds" in entry and "end_seconds" in entry:
        rate = entry.get("fps")
        if isinstance(rate, dict):
            rate = rate.get("fps")
        rate = float(rate or fps)
        return float(entry["start_seconds"]) * rate, float(entry["end_seconds"]) * rate
    return None


def apply_links(project_dir: str, key: str, entries: Optional[List[dict]] = None) -> dict:
    """Attach outside references to the clips of a snapshotted timeline.

    Each entry says which piece of footage it is about (`path`, plus `frames`
    {in, out} or `start_seconds`/`end_seconds`) and carries anything else worth
    remembering about it: a script scene and line, a search tool's clip id, a
    note. A moment from RavenSeek or a line from RavenWriter's edit list can be
    passed as it is, with extra keys added.

    An entry is matched to the timeline clip from the same file whose source
    range overlaps it most, so handles and later trims don't break the link.
    The result is `timelines/<key>/links.json`: {clip id: {the extra keys}}.

    Links for clips that have left the timeline are dropped; git keeps them.
    Returns {"given", "matched", "unmatched": [indexes of entries with no clip]}.
    """
    files = load_domains(project_dir, key)
    fps = float(files.get("metadata", {}).get("frame_rate") or 24.0)
    manifest = read_json(os.path.join(timeline_root(project_dir, key),
                                      "assets", "manifest.json")).get("assets", {})

    items = []
    for track in files.get("cuts", {}).get("video_tracks", []):
        for i in track.get("items", []):
            asset = manifest.get(i.get("media_ref"), {})
            items.append((i["id"], asset.get("original_path", ""),
                          asset.get("filename", ""),
                          i.get("source_start_frame", 0), i.get("source_end_frame", 0)))

    existing = read_links(project_dir, key)
    present = {item[0] for item in items}
    links = {k: v for k, v in existing.items() if k in present}

    matched, unmatched = 0, []
    for n, entry in enumerate(entries or []):
        if not isinstance(entry, dict):
            unmatched.append(n)
            continue
        path, filename = entry.get("path") or "", entry.get("file") or ""
        rng = _entry_range(entry, fps)
        best_id, best_overlap = None, 0.0
        for item_id, item_path, item_file, start, end in items:
            if path:
                if not item_path or not _same_file(path, item_path):
                    continue
            elif not filename or filename != item_file:
                continue
            overlap = (min(end, rng[1]) - max(start, rng[0])) if rng else 1.0
            if overlap > best_overlap:
                best_id, best_overlap = item_id, overlap
        if best_id is None:
            unmatched.append(n)
            continue
        links[best_id] = {k: v for k, v in entry.items() if k not in _LINK_MATCH_KEYS}
        matched += 1

    if links != existing:
        _write_json(_links_path(project_dir, key), links)
    return {"given": len(entries or []), "matched": matched, "unmatched": unmatched}


def write_journal(project_dir: str, message: str, timeline_name: str, key: str,
                  entry) -> str:
    """Write one journal entry. Returns its path relative to the project."""
    now = datetime.now(timezone.utc)
    journal_dir = os.path.join(project_dir, JOURNAL_DIR)
    base = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{_slug(message, 40)}"
    rel = f"{JOURNAL_DIR}/{base}.json"
    n = 2
    while os.path.exists(os.path.join(project_dir, rel)):
        rel = f"{JOURNAL_DIR}/{base}-{n}.json"
        n += 1
    os.makedirs(journal_dir, exist_ok=True)
    _write_json(os.path.join(project_dir, rel), {
        "time": now.isoformat(timespec="seconds"),
        "message": message,
        "timeline": timeline_name,
        "timeline_key": key,
        "entry": entry,
    })
    return rel


def _commit(project_dir: str, message: str, paths: List[str]) -> Optional[str]:
    """Stage and commit only `paths`. Returns the short hash, or None if unchanged."""
    _run(["add", "--"] + paths, cwd=project_dir)
    staged = _run(["diff", "--cached", "--quiet", "--"] + paths, cwd=project_dir, check=False)
    if staged.returncode == 0:
        return None

    args = []
    # A headless caller can't be prompted for an identity
    if not git_config_get(project_dir, "user.name"):
        args += ["-c", "user.name=vit snapshot"]
    if not git_config_get(project_dir, "user.email"):
        args += ["-c", "user.email=vit-snapshot@localhost"]
    _run(args + ["commit", "-m", message, "--"] + paths, cwd=project_dir)
    return _run(["rev-parse", "--short", "HEAD"], cwd=project_dir).stdout.strip()


def ensure_project(project_dir: str) -> bool:
    """Make `project_dir` a vit project if it isn't one. Returns True if it was created."""
    if os.path.isdir(os.path.join(project_dir, ".vit")):
        return False
    from .json_writer import write_timeline
    from .models import Timeline

    git_init(project_dir)
    write_timeline(project_dir, Timeline())
    _commit(project_dir, "vit: initial snapshot",
            [".vit", "timeline", "assets", ".gitignore"])
    return True


def snapshot(project_dir: str, timeline, project, message: str,
             resolve_app=None, color: bool = False, journal=None,
             links: Optional[List[dict]] = None) -> dict:
    """Record a timeline's current state and commit it.

    Args:
        project_dir: The vit project (a git repo with .vit/).
        timeline, project: Resolve objects.
        message: Commit message. Say what was done and why.
        resolve_app: The Resolve app object; only needed with color=True.
        color: Also export a LUT (and on Studio a DRX) per clip. Switches
            Resolve to the Color page and moves the playhead while it runs.
        journal: Any JSON-serialisable record of what was asked for (a tool
            call's arguments, a script's text), committed with the snapshot.
        links: Outside references to attach to clips; see apply_links().

    Returns a dict: timeline, key, commit (None if nothing was committed),
    changed (did the timeline itself change), domains_changed, diff, issues
    (from the validator), journal (path of the entry, if one was given), links
    (how many of the given links found their clip, if any were given).
    """
    if not message or not message.strip():
        raise SnapshotError("A snapshot needs a message.")

    if color:
        current = project.GetCurrentTimeline()
        if current is None or timeline_key(current) != timeline_key(timeline):
            raise SnapshotError(
                "Colour capture only works on the timeline that is open in "
                "Resolve. Open it, or snapshot without colour."
            )

    key = timeline_key(timeline)
    root = timeline_root(project_dir, key)
    os.makedirs(root, exist_ok=True)

    serialize_timeline(
        timeline, project, root,
        resolve_app=resolve_app if color else None,
        stable_ids=True,
        media_refs="path",
        capture_grades=color,
    )
    _update_index(project_dir, key, timeline, project)
    if links is not None and not isinstance(links, list):
        raise SnapshotError("Links must be a list of entries.")
    link_result = apply_links(project_dir, key, links)

    try:
        diff_text, domains_changed = diff_timeline(project_dir, key, "HEAD")
    except (GitError, ValueError):
        diff_text, domains_changed = "", []

    name = timeline.GetName() or ""
    paths = [f"{TIMELINES_DIR}/{key}", f"{TIMELINES_DIR}/{INDEX_FILE}"]
    journal_rel = None
    if journal is not None:
        journal_rel = write_journal(project_dir, message, name, key, journal)
        paths.append(journal_rel)

    commit = _commit(project_dir, message.strip(), paths)

    return {
        "timeline": name,
        "key": key,
        "commit": commit,
        "changed": bool(domains_changed),
        "domains_changed": domains_changed,
        "diff": diff_text,
        "issues": [str(i) for i in validate_project(root)],
        "journal": journal_rel,
        "links": link_result if links is not None else None,
    }


# --- Compact views: cheap for an assistant to read ----------------------

def diff_counts(diff_text: str) -> Dict[str, Dict[str, int]]:
    """Count a diff's lines per domain: {"cuts": {"added": 1, "removed": 0, "changed": 2}}."""
    counts: Dict[str, Dict[str, int]] = {}
    section = None
    kinds = {"+": "added", "-": "removed", "~": "changed"}
    for line in diff_text.splitlines():
        stripped = line.strip()
        if line.startswith("  ") and not line.startswith("   ") and stripped.endswith(":") \
                and stripped[:-1].isupper():
            section = stripped[:-1].lower()
            counts[section] = {"added": 0, "removed": 0, "changed": 0}
        elif section and line.startswith("  ") and line[2:3] in kinds and line[3:4] == " ":
            counts[section][kinds[line[2]]] += 1
    return counts


def cap_diff(diff_text: str, max_lines: int,
             hint: str = "use --full") -> Tuple[str, int, bool]:
    """Keep the first max_lines lines of a diff.

    Returns (text, total line count, whether it was cut). max_lines <= 0 keeps all.
    """
    lines = [l for l in diff_text.splitlines() if l.strip()]
    total = len(lines)
    if max_lines <= 0 or total <= max_lines:
        return "\n".join(lines), total, False
    kept = lines[:max_lines]
    kept.append(f"  … {total - max_lines} more lines ({hint})")
    return "\n".join(kept), total, True


def _tc_to_frames(tc: str, fps: float) -> int:
    try:
        hh, mm, ss, ff = (int(p) for p in tc.replace(";", ":").split(":"))
    except (ValueError, AttributeError):
        return 0
    return ((hh * 3600 + mm * 60 + ss) * int(round(fps))) + ff


def summarize_timeline(project_dir: str, key: str, ref: Optional[str] = None,
                       audio: bool = False) -> str:
    """One line per clip: the state of a timeline in as few words as will do.

    Reads the snapshot on disk, or the one at a git ref. Timecodes are
    timeline timecodes; "src" is the source range in frames; "←" is the
    clip's link `ref`, if it has one.
    """
    files = load_domains(project_dir, key, ref)
    meta = files.get("metadata", {})
    if not meta:
        where = f" at {ref}" if ref else ""
        raise SnapshotError(f"No snapshot of this timeline{where}.")
    fps = float(meta.get("frame_rate") or 24.0)
    links = read_links(project_dir, key, ref)

    def tc(frame):
        return _frames_to_timecode(frame, fps)

    video = files.get("cuts", {}).get("video_tracks", [])
    audio_tracks = files.get("audio", {}).get("audio_tracks", [])
    markers = files.get("markers", {}).get("markers", [])
    v_items = [i for t in video for i in t.get("items", [])]
    a_count = sum(len(t.get("items", [])) for t in audio_tracks)

    head = f"{meta.get('timeline_name', '')} — {fps:g} fps, {len(v_items)} video clips"
    if v_items:
        start = min(i["record_start_frame"] for i in v_items)
        end = max(i["record_end_frame"] for i in v_items)
        head += f", {tc(start)}–{tc(end)}"
    head += f", {a_count} audio clips, {len(markers)} markers"
    if ref:
        head += f"  [at {ref}]"
    lines = [head]

    for track in video:
        items = track.get("items", [])
        if not items:
            continue
        lines.append(f"V{track.get('index')}")
        for i in items:
            line = (f"  {tc(i['record_start_frame'])}–{tc(i['record_end_frame'])}  "
                    f"{i.get('name', '')}")
            if i.get("item_type", "media") != "media":
                text = (i.get("text_properties") or {}).get("styled_text", "")
                line += f"  [{i['item_type']}" + (f': "{text}"' if text else "") + "]"
            else:
                line += f"  src {i.get('source_start_frame')}–{i.get('source_end_frame')}"
            speed = (i.get("speed") or {}).get("speed_percent", 100.0)
            if speed != 100.0:
                line += f"  speed {speed:g}%"
            if i.get("clip_enabled") is False:
                line += "  (disabled)"
            link_ref = (links.get(i.get("id")) or {}).get("ref")
            if link_ref:
                line += f"  ← {link_ref}"
            lines.append(line)

    for track in audio_tracks:
        items = track.get("items", [])
        if not items:
            continue
        if not audio:
            lines.append(f"A{track.get('index')}  {len(items)} clips")
            continue
        lines.append(f"A{track.get('index')}")
        for i in items:
            lines.append(f"  {tc(i['start_frame'])}–{tc(i['end_frame'])}  "
                         f"vol {i.get('volume', 0.0):g} dB  pan {i.get('pan', 0.0):g}")

    if markers:
        # Marker frames count from the timeline's start; clip frames don't
        offset = _tc_to_frames(meta.get("start_timecode", ""), fps)
        lines.append("MARKERS")
        for m in markers:
            line = f"  {tc(offset + m['frame'])}  {m.get('color', '')}  \"{m.get('name', '')}\""
            if m.get("note"):
                line += f": {m['note']}"
            lines.append(line)

    return "\n".join(lines)


def _domain_of(path: str) -> Optional[str]:
    if "/grades/" in path:
        return "color"
    if "/generators/" in path:
        return "cuts"
    for domain, rel in DOMAIN_FILES.items():
        if path.endswith("/" + rel):
            return domain
    return None


def timeline_log(project_dir: str, key: str, max_count: int = 20) -> List[dict]:
    """History of one timeline: [{hash, date, message, domains}], newest first."""
    sep = "\x1f"
    result = _run(
        ["log", f"--max-count={max_count}", f"--pretty=format:%h{sep}%ad{sep}%s",
         "--date=format:%Y-%m-%d %H:%M", "--name-only", "--",
         f"{TIMELINES_DIR}/{key}"],
        cwd=project_dir, check=False,
    )
    if result.returncode != 0:
        return []
    commits: List[dict] = []
    for line in result.stdout.splitlines():
        if sep in line:
            h, date, message = line.split(sep, 2)
            commits.append({"hash": h, "date": date, "message": message, "domains": []})
        elif line.strip() and commits:
            domain = _domain_of(line.strip())
            if domain and domain not in commits[-1]["domains"]:
                commits[-1]["domains"].append(domain)
    order = list(DOMAIN_FILES)
    for c in commits:
        c["domains"].sort(key=order.index)
    return commits
