"""Accounts, passwords and session tokens.

The bar here is different from the rest of the suite. Elsewhere a bug costs a
bad transcript; here it costs someone else's meetings. So most of these tests
assert on what must be *refused* - a tampered cookie, a wrong password, the
deletion that would leave nobody able to manage accounts.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from meetbot.service.auth import (
    MAX_FAILED_ATTEMPTS,
    MIN_PASSWORD_LEN,
    SESSION_TTL_S,
    AuthError,
    Role,
    UserStore,
    bootstrap_admin,
    hash_password,
    issue_session,
    load_or_create_secret,
    normalise_username,
    read_session,
    verify_password,
)

PASSWORD = "correct-horse-battery"


@pytest.fixture
def store(tmp_path: Path) -> UserStore:
    return UserStore(tmp_path / "users.json")


class TestPasswordHashing:
    def test_round_trips(self) -> None:
        assert verify_password(PASSWORD, hash_password(PASSWORD))

    def test_rejects_the_wrong_password(self) -> None:
        assert not verify_password("nearly-right", hash_password(PASSWORD))

    def test_never_stores_the_password(self) -> None:
        assert PASSWORD not in hash_password(PASSWORD)

    def test_salts_every_hash(self) -> None:
        """Equal passwords must not produce equal hashes.

        Otherwise the database reveals which accounts share a password, and
        one cracked hash breaks all of them at once.
        """
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_refuses_a_short_password(self) -> None:
        with pytest.raises(AuthError, match=str(MIN_PASSWORD_LEN)):
            hash_password("x" * (MIN_PASSWORD_LEN - 1))

    @pytest.mark.parametrize(
        "corrupt",
        ["", "not-a-hash", "scrypt$bad", "md5$1$1$1$aaaa$bbbb", "scrypt$0$0$0$a$b"],
    )
    def test_a_corrupt_record_denies_rather_than_raises(self, corrupt: str) -> None:
        """A damaged row must not take the login endpoint down with it."""
        assert verify_password(PASSWORD, corrupt) is False


class TestUsernames:
    @pytest.mark.parametrize(
        "raw, expected",
        [("neil", "neil"), ("  Neil  ", "neil"), ("NEIL.S", "neil.s"),
         ("a_b-c.1", "a_b-c.1")],
    )
    def test_folds_to_a_canonical_form(self, raw: str, expected: str) -> None:
        assert normalise_username(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", "a", " ", "-neil", "neil sharma", "neil@x.com", "n" * 33, "n/../x"]
    )
    def test_rejects_unusable_names(self, raw: str) -> None:
        with pytest.raises(AuthError):
            normalise_username(raw)

    def test_case_cannot_create_a_lookalike_account(self, store: UserStore) -> None:
        """Two accounts rendering identically in the admin list is a trap."""
        store.add("neil", PASSWORD)
        with pytest.raises(AuthError, match="already exists"):
            store.add("NEIL", PASSWORD)


class TestAuthentication:
    def test_accepts_the_right_password(self, store: UserStore) -> None:
        store.add("neil", PASSWORD)
        user = store.authenticate("neil", PASSWORD)
        assert user is not None and user.username == "neil"

    def test_is_case_insensitive_in_the_username(self, store: UserStore) -> None:
        store.add("neil", PASSWORD)
        assert store.authenticate("NEIL", PASSWORD) is not None

    def test_rejects_the_wrong_password(self, store: UserStore) -> None:
        store.add("neil", PASSWORD)
        assert store.authenticate("neil", "wrong") is None

    def test_rejects_an_unknown_user(self, store: UserStore) -> None:
        assert store.authenticate("nobody", PASSWORD) is None

    def test_locks_out_after_repeated_failures(self, store: UserStore) -> None:
        """Rate-limits the one endpoint reachable without credentials."""
        store.add("neil", PASSWORD)
        for _ in range(MAX_FAILED_ATTEMPTS):
            assert store.authenticate("neil", "wrong") is None
        # Even the correct password is refused while locked out.
        assert store.authenticate("neil", PASSWORD) is None
        assert store.locked_out("neil") > 0

    def test_the_lockout_expires(self, store: UserStore) -> None:
        store.add("neil", PASSWORD)
        start = time.time()
        for _ in range(MAX_FAILED_ATTEMPTS):
            store.authenticate("neil", "wrong", now=start)
        assert store.authenticate("neil", PASSWORD, now=start + 301) is not None

    def test_a_successful_login_clears_the_count(self, store: UserStore) -> None:
        store.add("neil", PASSWORD)
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            store.authenticate("neil", "wrong")
        assert store.authenticate("neil", PASSWORD) is not None
        assert store.locked_out("neil") == 0


class TestUserStore:
    def test_survives_a_restart(self, tmp_path: Path) -> None:
        UserStore(tmp_path / "u.json").add("neil", PASSWORD, Role.ADMIN)
        reopened = UserStore(tmp_path / "u.json")
        assert reopened.authenticate("neil", PASSWORD) is not None
        assert reopened.get("neil").is_admin

    def test_lists_admins_first(self, store: UserStore) -> None:
        store.add("zoe", PASSWORD)
        store.add("amit", PASSWORD)
        store.add("neil", PASSWORD, Role.ADMIN)
        assert [u.username for u in store.list()] == ["neil", "amit", "zoe"]

    def test_changing_a_password_invalidates_the_old_one(
        self, store: UserStore
    ) -> None:
        store.add("neil", PASSWORD)
        store.set_password("neil", "a-brand-new-password")
        assert store.authenticate("neil", PASSWORD) is None
        assert store.authenticate("neil", "a-brand-new-password") is not None

    def test_will_not_delete_the_last_admin(self, store: UserStore) -> None:
        """Losing every admin means nobody can ever manage accounts again."""
        store.add("neil", PASSWORD, Role.ADMIN)
        store.add("member", PASSWORD)
        with pytest.raises(AuthError, match="only admin"):
            store.delete("neil")

    def test_will_not_demote_the_last_admin(self, store: UserStore) -> None:
        store.add("neil", PASSWORD, Role.ADMIN)
        with pytest.raises(AuthError, match="only admin"):
            store.set_role("neil", Role.MEMBER)

    def test_deletes_an_admin_when_another_remains(self, store: UserStore) -> None:
        store.add("neil", PASSWORD, Role.ADMIN)
        store.add("priya", PASSWORD, Role.ADMIN)
        store.delete("neil")
        assert store.get("neil") is None

    def test_an_unreadable_database_refuses_to_start(self, tmp_path: Path) -> None:
        """Starting empty would let the bootstrap mint an admin over the top."""
        path = tmp_path / "u.json"
        path.write_text("{ this is not json")
        with pytest.raises(ValueError):
            UserStore(path)


class TestBootstrap:
    def test_creates_a_first_admin(self, store: UserStore) -> None:
        created = bootstrap_admin(store)
        assert created is not None
        name, password = created
        user = store.authenticate(name, password)
        assert user is not None and user.is_admin

    def test_does_nothing_when_accounts_exist(self, store: UserStore) -> None:
        """A second admin appearing on restart would be a silent back door."""
        store.add("neil", PASSWORD, Role.ADMIN)
        assert bootstrap_admin(store) is None
        assert len(store) == 1


class TestSessions:
    @pytest.fixture
    def secret(self, tmp_path: Path) -> bytes:
        return load_or_create_secret(tmp_path / "session.key")

    def test_round_trips(self, secret: bytes) -> None:
        assert read_session(issue_session("neil", secret), secret) == "neil"

    def test_the_secret_persists_across_restarts(self, tmp_path: Path) -> None:
        """Otherwise every restart silently signs everyone out."""
        first = load_or_create_secret(tmp_path / "k")
        assert load_or_create_secret(tmp_path / "k") == first

    def test_rejects_a_tampered_token(self, secret: bytes) -> None:
        token = issue_session("neil", secret)
        assert read_session(token[:-3] + "aaa", secret) is None

    def test_a_forged_username_does_not_verify(self, secret: bytes) -> None:
        """The signature covers the name, so it cannot be swapped."""
        token = issue_session("member", secret)
        name_b64, expires, signature = token.split(".")
        import base64

        forged = base64.urlsafe_b64encode(b"admin").decode().rstrip("=")
        assert read_session(f"{forged}.{expires}.{signature}", secret) is None

    def test_rejects_another_server_key(self, secret: bytes) -> None:
        assert read_session(issue_session("neil", secret), b"x" * 48) is None

    def test_expires(self, secret: bytes) -> None:
        token = issue_session("neil", secret)
        assert read_session(token, secret, now=time.time() + SESSION_TTL_S + 1) is None

    @pytest.mark.parametrize("junk", ["", "a.b", "a.b.c.d", "....", "notatoken"])
    def test_garbage_is_just_no_session(self, junk: str, secret: bytes) -> None:
        assert read_session(junk, secret) is None
