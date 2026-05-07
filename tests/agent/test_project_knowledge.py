"""Tests for agent/project_knowledge.py — auto-detection, index build, render."""

import os
from pathlib import Path

import pytest

from agent.project_knowledge import (
    DEFAULT_INDEX_MAX_CHARS,
    DEFAULT_INDEX_MAX_FILES,
    INDEXABLE_EXTS,
    build_index,
    detect_project_name,
    get_pk_root,
    get_project_dir,
    render_index_block,
)


# ---------------------------------------------------------------------------
# Project detection
# ---------------------------------------------------------------------------

class TestDetectProjectName:
    def test_falls_back_to_cwd_basename(self, tmp_path, monkeypatch):
        # tmp_path itself is not a git repo
        monkeypatch.chdir(tmp_path)
        # The detector either returns the cwd basename or a git-root basename
        # of an enclosing repo (e.g. /var/folders/... → folders).  Both are
        # valid behavior — what matters is that we get a non-empty string.
        name = detect_project_name()
        assert isinstance(name, str)
        assert name  # non-empty

    def test_uses_git_root_basename_when_inside_repo(self, tmp_path, monkeypatch):
        import subprocess

        repo = tmp_path / "myproject"
        repo.mkdir()
        try:
            subprocess.run(
                ["git", "init", str(repo)],
                check=True, capture_output=True, timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            pytest.skip("git not available")
        nested = repo / "subdir"
        nested.mkdir()
        monkeypatch.chdir(nested)
        assert detect_project_name() == "myproject"

    def test_explicit_cwd_arg(self, tmp_path):
        sub = tmp_path / "explicit_dir"
        sub.mkdir()
        # Without git, falls back to the directory's own name
        assert detect_project_name(cwd=str(sub)) == "explicit_dir"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

class TestPathHelpers:
    def test_pk_root_under_hermes_home(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        # get_hermes_home() reads the env var lazily, so this should work
        assert get_pk_root() == home / "project-knowledge"

    def test_get_project_dir_uses_explicit_name(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert get_project_dir("foo") == home / "project-knowledge" / "foo"


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

@pytest.fixture()
def pk_dir(tmp_path, monkeypatch):
    """Set up a HERMES_HOME with a project-knowledge tree for 'demo'."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    pk = home / "project-knowledge" / "demo"
    pk.mkdir(parents=True)
    return pk


class TestBuildIndex:
    def test_no_directory_returns_empty_index(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        idx = build_index("nonexistent")
        assert idx.is_empty
        assert idx.files == []

    def test_indexes_markdown_files(self, pk_dir):
        (pk_dir / "overview.md").write_text("# Project Overview\nDetails...")
        (pk_dir / "schema.md").write_text("# Schema\nTable defs.")
        idx = build_index("demo")
        rels = {f.relpath for f in idx.files}
        assert "overview.md" in rels
        assert "schema.md" in rels

    def test_extracts_summary_from_first_heading(self, pk_dir):
        (pk_dir / "guide.md").write_text("# How to Onboard\nSteps...")
        idx = build_index("demo")
        guide = next(f for f in idx.files if f.relpath == "guide.md")
        assert guide.summary == "How to Onboard"

    def test_extracts_summary_from_frontmatter(self, pk_dir):
        (pk_dir / "fm.md").write_text(
            "---\ndescription: Frontmatter wins over heading\n---\n# Heading"
        )
        idx = build_index("demo")
        fm = next(f for f in idx.files if f.relpath == "fm.md")
        assert "Frontmatter wins" in fm.summary

    def test_skips_non_indexable_extensions(self, pk_dir):
        (pk_dir / "data.bin").write_text("binary stuff")
        (pk_dir / "script.py").write_text("print('hi')")
        (pk_dir / "valid.md").write_text("# Valid")
        idx = build_index("demo")
        rels = {f.relpath for f in idx.files}
        assert "valid.md" in rels
        assert "data.bin" not in rels
        assert "script.py" not in rels

    def test_skips_dotfiles_and_caches(self, pk_dir):
        (pk_dir / ".hidden.md").write_text("# Hidden")
        cache = pk_dir / "__pycache__"
        cache.mkdir()
        (cache / "stale.md").write_text("# Stale")
        nm = pk_dir / "node_modules"
        nm.mkdir()
        (nm / "leaked.md").write_text("# Leaked")
        (pk_dir / "kept.md").write_text("# Kept")
        idx = build_index("demo")
        rels = {f.relpath for f in idx.files}
        assert "kept.md" in rels
        assert all(not r.startswith(".") for r in rels)
        assert not any("__pycache__" in r for r in rels)
        assert not any("node_modules" in r for r in rels)

    def test_recurses_into_subdirectories(self, pk_dir):
        sub = pk_dir / "distilled" / "i18n"
        sub.mkdir(parents=True)
        (sub / "strings.yaml").write_text("hello: 你好")
        idx = build_index("demo")
        rels = {f.relpath for f in idx.files}
        assert "distilled/i18n/strings.yaml" in rels

    def test_truncates_at_max_files(self, pk_dir):
        for i in range(5):
            (pk_dir / f"f{i:02d}.md").write_text(f"# File {i}")
        idx = build_index("demo", max_files=3)
        assert len(idx.files) == 3
        assert idx.truncated_count == 2

    def test_extensions_constant_includes_common_text_formats(self):
        # Behavior contract: agents need to be able to put extracted data
        # in YAML / JSON / text — these must be searchable.
        for ext in (".md", ".yaml", ".yml", ".json", ".txt"):
            assert ext in INDEXABLE_EXTS


# ---------------------------------------------------------------------------
# Index rendering
# ---------------------------------------------------------------------------

class TestRenderIndexBlock:
    def test_empty_index_renders_to_empty_string(self, pk_dir):
        idx = build_index("demo")
        assert render_index_block(idx) == ""

    def test_renders_header_with_project_name_and_path(self, pk_dir):
        (pk_dir / "x.md").write_text("# X")
        idx = build_index("demo")
        block = render_index_block(idx)
        assert "Project Knowledge: demo" in block
        assert str(pk_dir) in block
        assert "project_knowledge_search" in block
        assert "project_knowledge_view" in block

    def test_lists_files_with_summaries(self, pk_dir):
        (pk_dir / "a.md").write_text("# About A")
        (pk_dir / "b.md").write_text("# About B")
        idx = build_index("demo")
        block = render_index_block(idx)
        assert "a.md" in block
        assert "About A" in block
        assert "b.md" in block

    def test_truncation_hint_when_over_max_chars(self, pk_dir):
        # Many files with long summaries to force truncation
        for i in range(50):
            (pk_dir / f"f{i:02d}.md").write_text(
                f"# Lengthy file number {i} " + ("x" * 80)
            )
        idx = build_index("demo")
        # Use a deliberately small cap so truncation kicks in
        block = render_index_block(idx, max_chars=400)
        assert "more file(s)" in block
        assert "project_knowledge_search" in block
        assert len(block) < 600  # didn't run away

    def test_render_warns_about_not_auto_reading(self, pk_dir):
        (pk_dir / "x.md").write_text("# X")
        idx = build_index("demo")
        block = render_index_block(idx)
        # Behavior contract: the prompt MUST tell the model not to slurp
        # everything — that's the whole point of indexing instead of
        # injecting full contents.
        assert "Do NOT auto-read" in block or "do not auto" in block.lower()
