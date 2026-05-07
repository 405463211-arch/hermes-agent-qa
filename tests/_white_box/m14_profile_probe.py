"""M14 white-box profile isolation probe.

Profiles let users run multiple Hermes instances side-by-side, each with
its own ``HERMES_HOME``. The contract:

  - All state (memory, learning store, sessions, plugins) lives under
    HERMES_HOME and never leaks across profiles.
  - Switching HERMES_HOME mid-process must take effect for the next
    MemoryStore/LearningStore instantiation.
  - get_hermes_home() / get_memory_dir() / get_learning_store_path()
    are the single sources of truth.

This probe tests by directly toggling HERMES_HOME between two temp
profiles and verifying state is isolated. We don't spawn separate
processes here — that's M12. Here we want to verify the
single-process API contract.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """Set up two HERMES_HOME directories. Returns (home_a, home_b,
    activate(name)) where activate switches the active profile."""
    home_a = tmp_path / ".hermes-a"
    home_b = tmp_path / ".hermes-b"
    home_a.mkdir(parents=True)
    home_b.mkdir(parents=True)

    def activate(which: str) -> Path:
        target = home_a if which == "a" else home_b
        monkeypatch.setenv("HERMES_HOME", str(target))
        return target

    return home_a, home_b, activate


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ get_hermes_home() respects HERMES_HOME                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestHermesHomeResolution:
    def test_get_hermes_home_reads_env(self, two_profiles):
        from hermes_constants import get_hermes_home
        home_a, home_b, activate = two_profiles

        activate("a")
        assert get_hermes_home() == home_a

        activate("b")
        assert get_hermes_home() == home_b

    def test_get_memory_dir_follows_profile(self, two_profiles):
        from tools.memory_tool import get_memory_dir
        home_a, home_b, activate = two_profiles

        activate("a")
        assert get_memory_dir() == home_a / "memories"

        activate("b")
        assert get_memory_dir() == home_b / "memories"

    def test_get_learning_store_path_follows_profile(self, two_profiles):
        from agent.learning_store import get_learning_store_path
        home_a, home_b, activate = two_profiles

        activate("a")
        assert get_learning_store_path() == home_a / "learning_store.db"

        activate("b")
        assert get_learning_store_path() == home_b / "learning_store.db"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ MemoryStore isolation between profiles                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestMemoryStoreIsolation:
    def test_writes_to_profile_a_invisible_in_profile_b(self, two_profiles):
        from tools.memory_tool import MemoryStore
        home_a, home_b, activate = two_profiles

        activate("a")
        store_a = MemoryStore()
        store_a.load_from_disk()
        result = store_a.add("rules", "rule from profile A")
        assert result.get("success")

        # Confirm RULES.md was written under profile A's home
        rules_a = home_a / "memories" / "RULES.md"
        assert rules_a.exists()
        assert "rule from profile A" in rules_a.read_text()

        # Switch to profile B
        activate("b")
        store_b = MemoryStore()
        store_b.load_from_disk()
        # Profile B should be empty
        assert not store_b.rules_entries

        # Profile B's RULES.md should not exist (or be empty)
        rules_b = home_b / "memories" / "RULES.md"
        assert not rules_b.exists() or not rules_b.read_text().strip()

        # Profile A's RULES.md is untouched after switching
        assert "rule from profile A" in rules_a.read_text()

    def test_each_profile_has_independent_archive(self, two_profiles):
        from tools.memory_tool import MemoryStore
        home_a, home_b, activate = two_profiles

        activate("a")
        store_a = MemoryStore(
            rules_char_limit=100,
            auto_archive_rules=True,
            auto_archive_capacity_threshold=0.5,
        )
        store_a.load_from_disk()
        for i in range(20):
            store_a.add("rules", f"rule a-{i} with some text padding")
        store_a.run_auto_archive()
        archive_a = home_a / "memories" / "RULES.archive.md"

        activate("b")
        store_b = MemoryStore()
        store_b.load_from_disk()
        archive_b = home_b / "memories" / "RULES.archive.md"

        # Profile A's archive must NOT appear in profile B's tree
        if archive_a.exists():
            assert not archive_b.exists() or (
                archive_a.read_text() != archive_b.read_text()
            ), "archives are bleeding across profiles"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ LearningStore isolation                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestLearningStoreIsolation:
    def test_each_profile_has_separate_db(self, two_profiles):
        from agent.learning_store import LearningStore, get_learning_store_path
        home_a, home_b, activate = two_profiles

        activate("a")
        path_a = get_learning_store_path()
        store_a = LearningStore(db_path=path_a)
        store_a.record(
            category="error",
            pattern_key="profile-a-key",
            summary="from profile A",
        )

        activate("b")
        path_b = get_learning_store_path()
        store_b = LearningStore(db_path=path_b)
        active_b = store_b.list(status="pending", limit=100)
        assert not any(e.get("pattern_key") == "profile-a-key" for e in active_b), (
            "profile A's learning entry leaked into profile B"
        )

        # Different file paths
        assert path_a != path_b
        assert path_a.exists()
        # path_b may or may not exist depending on connect timing — just
        # ensure the contents are isolated, which we did above

    def test_db_path_uses_get_hermes_home_not_path_home(self):
        """Static contract: get_learning_store_path must call
        get_hermes_home(), not Path.home() / ".hermes" — otherwise
        profiles would all share the same db."""
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent.parent
        src = (repo_root / "agent" / "learning_store.py").read_text()
        # Find the function body
        idx = src.find("def get_learning_store_path")
        assert idx > 0
        body = src[idx : idx + 400]
        assert "get_hermes_home()" in body, (
            "get_learning_store_path must use get_hermes_home() for "
            "profile compatibility"
        )
        # Check no hardcoded Path.home() / ".hermes" / "learning..."
        # in the body. (Other parts of the file may use Path.home() for
        # legitimate reasons, e.g. comments.)
        assert 'Path.home() / ".hermes"' not in body, (
            "get_learning_store_path hardcodes ~/.hermes — breaks profiles"
        )

    def test_memory_dir_uses_get_hermes_home_not_path_home(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent.parent
        src = (repo_root / "tools" / "memory_tool.py").read_text()
        idx = src.find("def get_memory_dir")
        assert idx > 0
        body = src[idx : idx + 200]
        assert "get_hermes_home()" in body
        assert 'Path.home() / ".hermes"' not in body


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ Cross-profile config / plugins do not bleed                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝


class TestConfigIsolation:
    def test_config_path_is_profile_scoped(self, two_profiles):
        from hermes_constants import get_config_path
        home_a, home_b, activate = two_profiles

        activate("a")
        cfg_a = get_config_path()
        assert cfg_a == home_a / "config.yaml"

        activate("b")
        cfg_b = get_config_path()
        assert cfg_b == home_b / "config.yaml"
        assert cfg_a != cfg_b

    def test_skills_dir_is_profile_scoped(self, two_profiles):
        from hermes_constants import get_skills_dir
        home_a, home_b, activate = two_profiles

        activate("a")
        skills_a = get_skills_dir()
        assert skills_a == home_a / "skills"

        activate("b")
        skills_b = get_skills_dir()
        assert skills_b == home_b / "skills"
