"""The repository is meant to be public: no machine-specific absolute paths in tracked files."""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r"/workspace/|/home/box\b|/tmp/fg-e2e-\w")


def tracked_files():
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [REPO / f for f in out.splitlines() if f and f != "tests/test_public_ready.py"]


def test_no_machine_specific_paths_in_tracked_files():
    hits = []
    for p in tracked_files():
        try:
            text = p.read_text(errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        hits += [f"{p.relative_to(REPO)}:{n}" for n, line in enumerate(text.splitlines(), 1) if FORBIDDEN.search(line)]
    assert hits == []


def test_agent_config_examples_are_placeholders():
    for name in ("claude-code.mcp.json", "codex.config.toml"):
        assert "/path/to/flagsmith/.venv/bin/fg-mcp" in (REPO / "examples" / name).read_text()
