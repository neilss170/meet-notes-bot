"""Per-run copies of the signed-in Chromium profile.

The bug these exist for is quiet rather than loud: Chromium hands a second
browser opening the same profile directory a *blank* profile instead of an
error, so the only visible symptom is a bot that mysteriously cannot join.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meetbot.profile import SKIP_DIRS, clone_profile, discard_profile


@pytest.fixture
def master(tmp_path: Path) -> Path:
    """A profile shaped like the real one: session state buried in cache."""
    profile = tmp_path / "master"
    (profile / "Default" / "Network").mkdir(parents=True)
    (profile / "Default" / "Network" / "Cookies").write_text("session-state")
    (profile / "Default" / "Preferences").write_text("{}")
    (profile / "Local State").write_text("{}")
    for name in ("Cache", "Code Cache", "GPUCache"):
        (profile / "Default" / name).mkdir()
        (profile / "Default" / name / "blob").write_text("x" * 1000)
    (profile / "SingletonLock").write_text("pid-of-a-dead-browser")
    return profile


class TestCloneProfile:
    def test_carries_the_signed_in_session(self, master: Path) -> None:
        """The whole point: the copy is still logged in."""
        clone = clone_profile(master)
        assert (clone / "Default" / "Network" / "Cookies").read_text() == "session-state"
        assert (clone / "Local State").exists()

    def test_two_clones_are_independent(self, master: Path) -> None:
        """Two concurrent meetings must not share mutable state.

        Sharing the directory is exactly the bug: Chromium gave the second
        browser an empty profile, so the bot joined signed-out and Meet
        refused it as an anonymous guest.
        """
        a, b = clone_profile(master), clone_profile(master)
        assert a != b
        (a / "Default" / "Network" / "Cookies").write_text("meeting-a wrote this")
        assert (b / "Default" / "Network" / "Cookies").read_text() == "session-state"

    def test_leaves_the_master_untouched(self, master: Path) -> None:
        """A crashed run must not damage the profile every future run needs."""
        clone = clone_profile(master)
        (clone / "Default" / "Network" / "Cookies").write_text("corrupted")
        (clone / "Local State").unlink()
        assert (master / "Default" / "Network" / "Cookies").read_text() == "session-state"
        assert (master / "Local State").exists()

    def test_skips_regenerable_cache(self, master: Path) -> None:
        """Copying cache would put ~50 MB of pointless IO in the join path."""
        clone = clone_profile(master)
        for name in ("Cache", "Code Cache", "GPUCache"):
            assert name in SKIP_DIRS
            assert not (clone / "Default" / name).exists()

    def test_does_not_carry_over_a_lock(self, master: Path) -> None:
        """A copied Singleton file claims the profile for a dead process."""
        assert not (clone_profile(master) / "SingletonLock").exists()

    def test_lands_inside_the_requested_parent(self, tmp_path: Path, master: Path) -> None:
        parent = tmp_path / "clones"
        parent.mkdir()
        assert clone_profile(master, parent).parent == parent

    def test_a_missing_master_says_to_log_in(self, tmp_path: Path) -> None:
        """Not signed in yet is a setup step, not an unexplained crash."""
        with pytest.raises(FileNotFoundError, match="meetbot login"):
            clone_profile(tmp_path / "never-created")


class TestDiscardProfile:
    def test_removes_the_clone(self, master: Path) -> None:
        clone = clone_profile(master)
        discard_profile(clone)
        assert not clone.exists()

    def test_is_safe_to_call_twice(self, master: Path) -> None:
        clone = clone_profile(master)
        discard_profile(clone)
        discard_profile(clone)

    def test_tolerates_none(self) -> None:
        """close() runs after partial startup, where no clone was ever made."""
        discard_profile(None)

    def test_a_locked_clone_does_not_raise(self, master: Path, tmp_path: Path) -> None:
        """Windows can hold profile files open just after the browser exits.

        A meeting that has already written its transcript must not fail
        during cleanup; a leaked temp directory is the lesser problem.
        """
        clone = clone_profile(master)
        handle = (clone / "Default" / "Network" / "Cookies").open("w")
        try:
            discard_profile(clone)   # must not raise
        finally:
            handle.close()
            discard_profile(clone)
