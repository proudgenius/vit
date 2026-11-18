"""Tests for the MCP server (vit/mcp_server.py) and the MCP config builder."""

import io
import json
import os

import pytest

from tests.mock_resolve import MockMediaPoolItem, MockTimelineItem, create_test_timeline
from vit import mcp_config
from vit.mcp_server import TOOLS, serve
from vit.snapshot import ensure_project


class Session:
    """Drives the server over in-memory streams, one request at a time."""

    def __init__(self, project_dir, resolve=None, project=None):
        self.project_dir = project_dir
        self.resolve, self.project = resolve, project
        self._id = 0

    def _connect(self):
        if self.project is None:
            raise RuntimeError("Resolve isn't reachable.")
        return self.resolve, self.project

    def send(self, method, params=None, notification=False):
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notification:
            msg["id"] = self._id
        out = io.BytesIO()
        serve(self.project_dir, stdin=io.BytesIO((json.dumps(msg) + "\n").encode()),
              stdout=out, connect=self._connect)
        lines = out.getvalue().decode().splitlines()
        return json.loads(lines[0]) if lines else None

    def call(self, name, **arguments):
        result = self.send("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["content"][0]["text"], result["isError"]


@pytest.fixture
def session(tmp_path):
    resolve, project, _ = create_test_timeline()
    return Session(str(tmp_path / "record"), resolve, project)


def test_initialize_and_tool_list(session):
    init = session.send("initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert init["serverInfo"]["name"] == "vit"
    assert "snapshot" in init["instructions"]
    assert session.send("initialize", {"protocolVersion": "1999-01-01"})["result"][
        "protocolVersion"] == "2025-06-18"

    tools = session.send("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["snapshot", "summary", "history", "diff", "timelines"]
    for t in tools:
        assert t["inputSchema"]["type"] == "object"


def test_notifications_get_no_reply_and_unknown_methods_get_an_error(session):
    assert session.send("notifications/initialized", notification=True) is None
    assert session.send("resources/list")["error"]["code"] == -32601
    assert session.send("ping")["result"] == {}


def test_server_creates_the_record_folder(session):
    session.send("ping")
    assert os.path.isdir(os.path.join(session.project_dir, ".vit"))
    assert os.path.isdir(os.path.join(session.project_dir, ".git"))


def _commit_of(text):
    return [l for l in text.splitlines() if l.startswith("commit: ")][0].split(": ")[1]


def test_snapshot_then_read_back(session):
    first, is_error = session.call("snapshot", message="built rough cut v1", links=[
        {"path": "/Volumes/Media/Interview_A_001.mov", "frames": {"in": 100, "out": 820},
         "ref": "scene 2 line 4"}])
    assert not is_error
    assert first.splitlines()[0] == "timeline: Main Edit v3"
    assert "changed: cuts (+2)" in first
    assert "links: 1 of 1 matched a clip" in first

    timeline = session.project.GetCurrentTimeline()
    timeline._audio_tracks[1][0].SetProperty("Volume", -9.0)
    second, _ = session.call("snapshot", message="lower level",
                             journal={"tool": "run_script"})
    assert "changed: audio (~1)" in second
    assert "volume" in second

    summary, _ = session.call("summary")
    assert "Interview_A_001  src 100–820  ← scene 2 line 4" in summary

    history, _ = session.call("history")
    assert history.splitlines()[0].endswith("[audio]  lower level")

    diff, _ = session.call("diff", ref=_commit_of(first))
    assert "volume" in diff

    assert "Main Edit v3" in session.call("timelines")[0]


def test_unchanged_snapshot_is_small(session):
    session.call("snapshot", message="first")
    text, _ = session.call("snapshot", message="session start")
    assert text == "timeline: Main Edit v3\ncommit: none\nchanged: nothing"


def test_unmatched_links_are_reported(session):
    text, _ = session.call("snapshot", message="first", links=[
        {"path": "/nowhere.mov", "ref": "scene 9"}])
    assert "links: 0 of 1 matched a clip; no clip for entries [0]" in text


def test_first_snapshot_of_a_long_timeline_is_capped(session):
    timeline = session.project.GetCurrentTimeline()
    for i in range(60):
        timeline._video_tracks[1].append(MockTimelineItem(
            name=f"clip{i}", start=1000 + i * 10, end=1010 + i * 10,
            media_pool_item=MockMediaPoolItem(f"/m/{i}.mov")))
    text, _ = session.call("snapshot", message="first")
    assert "cuts (+62)" in text
    assert len(text.splitlines()) <= 3 + 21
    assert "max_lines" in text.splitlines()[-1]


def test_errors_come_back_as_tool_errors_in_plain_words(tmp_path):
    offline = Session(str(tmp_path / "record"))
    text, is_error = offline.call("snapshot", message="x")
    assert is_error and "Resolve isn't reachable" in text

    text, is_error = offline.call("summary")
    assert is_error and "No timeline has a snapshot yet" in text

    text, is_error = offline.call("nope")
    assert is_error and "no tool called" in text


def test_prints_from_vit_code_stay_off_the_protocol_stream(session, capsys):
    session.call("snapshot", message="first")
    # serve() swaps sys.stdout for stderr while it runs; nothing leaks to stdout
    assert capsys.readouterr().out == ""


# --- config -------------------------------------------------------------

def test_templates_in_the_repo_match_the_builder():
    root = os.path.join(os.path.dirname(__file__), "..", "templates")
    for platform in ("windows", "macos", "linux"):
        with open(os.path.join(root, f"mcp.{platform}.json")) as f:
            assert json.load(f) == mcp_config.build(platform=platform)


def test_built_config_has_all_four_servers():
    config = mcp_config.build(platform="macos", python="/v/bin/python", project_dir="/rec")
    servers = config["mcpServers"]
    assert list(servers) == ["davinci-resolve", "ravenseek", "ravenwriter", "vit"]
    assert servers["vit"] == {"type": "stdio", "command": "/v/bin/python",
                              "args": ["-m", "vit", "mcp", "-C", "/rec"]}
    assert servers["ravenseek"]["args"] == ["--mcp"]
    assert mcp_config.placeholders_left(config) == ["ravenwriter"]


def test_merge_keeps_what_other_setups_wrote(tmp_path):
    path = str(tmp_path / ".mcp.json")
    mine = {"mcpServers": {
        "davinci-resolve": {"type": "stdio", "command": "/custom/ResolveMCP"},
        "other": {"command": "x"}}, "extra": 1}
    with open(path, "w") as f:
        json.dump(mine, f)

    config = mcp_config.build(platform="macos", python="/v/python", project_dir="/rec")
    written, kept, backup = mcp_config.merge_into(path, config)
    assert written == ["ravenseek", "ravenwriter", "vit"]
    assert kept == ["davinci-resolve"]
    with open(backup) as f:
        assert json.load(f) == mine

    with open(path) as f:
        merged = json.load(f)
    assert merged["extra"] == 1
    assert merged["mcpServers"]["other"] == {"command": "x"}
    assert merged["mcpServers"]["davinci-resolve"]["command"] == "/custom/ResolveMCP"

    # Running it again updates vit, leaves the rest, and keeps the first backup
    config = mcp_config.build(platform="macos", python="/v/python", project_dir="/rec2")
    written, kept, backup = mcp_config.merge_into(path, config)
    assert written == ["vit"] and backup is None
    with open(path) as f:
        assert json.load(f)["mcpServers"]["vit"]["args"][-1] == "/rec2"


def test_merge_creates_a_missing_file(tmp_path):
    path = str(tmp_path / "sub" / ".mcp.json")
    written, kept, backup = mcp_config.merge_into(
        path, mcp_config.build(platform="windows", only_vit=True))
    assert written == ["vit"] and kept == [] and backup is None
    assert os.path.exists(path)


def test_ensure_project_is_idempotent(tmp_path):
    d = str(tmp_path / "rec")
    assert ensure_project(d) is True
    assert ensure_project(d) is False
