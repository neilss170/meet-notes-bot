"""Accounts, passwords and sessions for the web service.

The service can send a bot into any meeting and read every transcript it has
ever produced, so until now it was safe only because it bound to loopback.
This module is what lets it be reachable by other people.

Local accounts, not Google sign-in. Restricting an OAuth app to a company
domain is the better answer once this belongs to IT - it removes passwords
entirely and revokes with the employee's account - but it needs a Google
Cloud project, a client secret and registered redirect URIs before anyone can
log in even once. Local accounts work on a laptop with nothing configured,
which is what a pilot needs, and the roles here map onto OAuth groups later
without changing any caller.

Passwords are hashed with scrypt from the standard library: memory-hard, so a
stolen database resists offline cracking, and no new dependency. Sessions are
HMAC-signed cookies rather than server-side state, so restarting the service
does not log everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

# -- passwords -------------------------------------------------------------

#: scrypt cost. n=2^15 costs ~32 MB and ~100 ms per verification, which is a
#: rounding error on a login and a serious obstacle to bulk cracking.
_SCRYPT_N: Final[int] = 1 << 15
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1
_SALT_BYTES: Final[int] = 16
_KEY_BYTES: Final[int] = 32

#: Shortest password accepted. Long enough that the scrypt cost, rather than
#: the search space, is what stands between an attacker and the account.
MIN_PASSWORD_LEN: Final[int] = 8

_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

#: Failed attempts, per username, before it is refused for a while. Guards
#: the one endpoint an attacker can reach without credentials.
MAX_FAILED_ATTEMPTS: Final[int] = 8
LOCKOUT_S: Final[float] = 300.0

SESSION_COOKIE: Final[str] = "meetbot_session"
SESSION_TTL_S: Final[float] = 12 * 60 * 60


class AuthError(ValueError):
    """Raised for a rejected account operation, safe to show a caller."""


class Role(str, Enum):
    """What an account may do."""

    #: Sees every meeting, and manages accounts.
    ADMIN = "admin"
    #: Sends the bot, and sees only the meetings it started.
    MEMBER = "member"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """Hash ``password`` for storage, salt and parameters included.

    The parameters travel with the hash, so raising the cost later does not
    invalidate existing accounts.
    """
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_BYTES,
        maxmem=_SCRYPT_N * _SCRYPT_R * 256,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, encoded: str) -> bool:
    """Check ``password`` against a stored hash, in constant time.

    Returns ``False`` for a malformed record rather than raising: a corrupt
    row should deny access, not take the login endpoint down.
    """
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        n_i, r_i, p_i = int(n), int(r), int(p)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=n_i,
            r=r_i,
            p=p_i,
            dklen=len(_unb64(expected)),
            maxmem=n_i * r_i * 256,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(candidate, _unb64(expected))


# -- accounts --------------------------------------------------------------


@dataclass(frozen=True)
class User:
    """One account. Never carries the password hash out of the store."""

    username: str
    role: Role
    created_at: float

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "role": self.role.value,
            "created_at": self.created_at,
        }


def normalise_username(raw: str) -> str:
    """Fold a username to its canonical form, or reject it.

    Case-insensitive, so "Neil" and "neil" cannot become two accounts that
    look identical in the admin list.
    """
    name = raw.strip().lower()
    if not _USERNAME_RE.match(name):
        raise AuthError(
            "Username must be 2-32 characters: letters, digits, dot, dash or "
            "underscore, starting with a letter or digit"
        )
    return name


class UserStore:
    """The account database: a JSON file, read and written whole.

    Small enough that anything cleverer would be premature - an office has
    tens of accounts, not millions - and a plain file stays inspectable when
    something goes wrong.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._users: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, list[float]] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._users = dict(data.get("users", {}))
        except (OSError, ValueError):
            # Refusing to start is the right failure for an unreadable
            # account database: starting empty would let the bootstrap mint
            # a fresh admin on top of accounts that still exist.
            logger.exception("Could not read the account database at %s", self.path)
            raise

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"users": self._users}, indent=2), encoding="utf-8")
        os.replace(temp, self.path)
        restrict_permissions(self.path)

    # -- queries -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._users)

    def get(self, username: str) -> User | None:
        name = username.strip().lower()
        record = self._users.get(name)
        if record is None:
            return None
        return User(
            username=name,
            role=Role(record.get("role", Role.MEMBER.value)),
            created_at=float(record.get("created_at", 0.0)),
        )

    def list(self) -> list[User]:
        """Every account, admins first then alphabetical."""
        users = [u for name in self._users if (u := self.get(name)) is not None]
        return sorted(users, key=lambda u: (not u.is_admin, u.username))

    def admin_count(self) -> int:
        return sum(1 for user in self.list() if user.is_admin)

    # -- commands ----------------------------------------------------------

    def add(self, username: str, password: str, role: Role = Role.MEMBER) -> User:
        """Create an account.

        Raises:
            AuthError: If the name is unusable or taken, or the password is
                too short.
        """
        name = normalise_username(username)
        if name in self._users:
            raise AuthError(f"User {name!r} already exists")
        self._users[name] = {
            "password": hash_password(password),
            "role": Role(role).value,
            "created_at": time.time(),
        }
        self._save()
        logger.info("Created %s account %r", Role(role).value, name)
        user = self.get(name)
        assert user is not None
        return user

    def set_password(self, username: str, password: str) -> None:
        name = normalise_username(username)
        if name not in self._users:
            raise AuthError(f"No such user {name!r}")
        self._users[name]["password"] = hash_password(password)
        self._failures.pop(name, None)
        self._save()
        logger.info("Password changed for %r", name)

    def set_role(self, username: str, role: Role) -> None:
        name = normalise_username(username)
        if name not in self._users:
            raise AuthError(f"No such user {name!r}")
        user = self.get(name)
        if user is not None and user.is_admin and Role(role) is not Role.ADMIN:
            self._require_another_admin(name)
        self._users[name]["role"] = Role(role).value
        self._save()

    def delete(self, username: str) -> None:
        """Remove an account, unless it is the last way in."""
        name = normalise_username(username)
        if name not in self._users:
            raise AuthError(f"No such user {name!r}")
        user = self.get(name)
        if user is not None and user.is_admin:
            self._require_another_admin(name)
        del self._users[name]
        self._failures.pop(name, None)
        self._save()
        logger.info("Deleted account %r", name)

    def _require_another_admin(self, name: str) -> None:
        """Guard against locking everyone out of account management."""
        if self.admin_count() <= 1:
            raise AuthError(
                f"{name!r} is the only admin. Promote another account first, "
                "or nobody will be able to manage users."
            )

    # -- authentication ----------------------------------------------------

    def locked_out(self, username: str, now: float | None = None) -> float:
        """Seconds remaining before ``username`` may try again (0 if free)."""
        now = time.time() if now is None else now
        recent = [t for t in self._failures.get(username, []) if now - t < LOCKOUT_S]
        self._failures[username] = recent
        if len(recent) < MAX_FAILED_ATTEMPTS:
            return 0.0
        return LOCKOUT_S - (now - recent[-MAX_FAILED_ATTEMPTS])

    def authenticate(
        self, username: str, password: str, now: float | None = None
    ) -> User | None:
        """Return the user if the password is right, else ``None``.

        An unknown username still pays the full hashing cost, so response
        time does not reveal which accounts exist.
        """
        name = username.strip().lower()
        if self.locked_out(name, now):
            return None
        record = self._users.get(name)
        if record is None:
            # Deliberate: verify against a throwaway hash so a missing
            # account takes as long as a wrong password does.
            verify_password(password, _DUMMY_HASH)
            self._note_failure(name, now)
            return None
        if not verify_password(password, record.get("password", "")):
            self._note_failure(name, now)
            logger.warning("Failed login for %r", name)
            return None
        self._failures.pop(name, None)
        return self.get(name)

    def _note_failure(self, name: str, now: float | None = None) -> None:
        self._failures.setdefault(name, []).append(time.time() if now is None else now)


#: A real hash of a value nobody can supply, used to make an unknown-username
#: login cost the same as a wrong-password one.
_DUMMY_HASH: Final[str] = hash_password(secrets.token_urlsafe(32))


# -- sessions --------------------------------------------------------------


def restrict_permissions(path: Path) -> None:
    """Best-effort owner-only permissions. A no-op where unsupported."""
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        logger.debug("Could not restrict permissions on %s", path)


def load_or_create_secret(path: Path) -> bytes:
    """Read the session-signing key, generating it on first run.

    Persisted rather than random per process, so restarting the service does
    not invalidate everyone's cookie.
    """
    path = Path(path).expanduser()
    if path.exists():
        secret = path.read_bytes().strip()
        if len(secret) >= 32:
            return secret
        logger.warning("Session secret at %s is too short; regenerating", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(48)
    path.write_bytes(secret)
    restrict_permissions(path)
    return secret


def issue_session(username: str, secret: bytes, ttl_s: float = SESSION_TTL_S) -> str:
    """Mint a signed session token for ``username``."""
    expires = int(time.time() + ttl_s)
    payload = f"{_b64(username.encode('utf-8'))}.{expires}"
    signature = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64(signature)}"


def read_session(token: str, secret: bytes, now: float | None = None) -> str | None:
    """Return the username a token proves, or ``None`` if it proves nothing.

    Tampering, a wrong key and expiry are all the same answer: no session.
    """
    now = time.time() if now is None else now
    try:
        name_b64, expires_raw, signature = token.split(".")
        payload = f"{name_b64}.{expires_raw}"
        expected = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature)):
            return None
        if now > int(expires_raw):
            return None
        return _unb64(name_b64).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def bootstrap_admin(store: UserStore) -> tuple[str, str] | None:
    """Create a first admin if the database is empty.

    Returns ``(username, password)`` when one was created, so the caller can
    show the generated password once. A service with no accounts would
    otherwise serve a login page nobody can get past.
    """
    if len(store):
        return None
    password = secrets.token_urlsafe(12)
    store.add("admin", password, Role.ADMIN)
    return "admin", password


__all__ = [
    "LOCKOUT_S",
    "MAX_FAILED_ATTEMPTS",
    "MIN_PASSWORD_LEN",
    "SESSION_COOKIE",
    "SESSION_TTL_S",
    "AuthError",
    "Role",
    "User",
    "UserStore",
    "bootstrap_admin",
    "hash_password",
    "issue_session",
    "load_or_create_secret",
    "normalise_username",
    "read_session",
    "restrict_permissions",
    "verify_password",
]
