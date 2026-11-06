"""Shared account credentials backed by the desktop's secure keyring.

No secret is written to SQLite, settings, or a plaintext fallback. Keyring calls
may unlock a desktop service, so callers must perform get/save/forget on a worker.
Constructing this class and setting a session override do no external I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
from threading import Lock, RLock
from urllib.parse import urlsplit


SERVICE = "debacal.calendar-account.v1"


class CredentialError(ValueError):
    """An account credential could not be read or persisted securely."""


def account_key(calendar_or_url, username: str | None = None) -> str:
    """Share by normalized server origin/login, isolating Nextcloud deployments."""
    if hasattr(calendar_or_url, "url"):
        url = calendar_or_url.url
        username = calendar_or_url.username if username is None else username
    else:
        url = str(calendar_or_url)
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (ValueError, UnicodeError):
        raise CredentialError("Use an HTTP(S) account URL without embedded credentials.") from None
    deployment = ""
    marker = re.search(r"/(?:remote|dav)\.php(?:/|$)", parsed.path, re.I)
    if marker:
        deployment = parsed.path[:marker.start()].rstrip("/")
    identity = [parsed.scheme.lower(), host, port, deployment, (username or "").strip()]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class CredentialStore:
    def __init__(self, backend=None):
        # Injected backends are for integrations/tests. Automatic selection only
        # admits OS services, even if a plaintext keyrings.alt plugin is installed.
        self._backend = backend
        self._cache_lock = RLock()
        self._io_lock = Lock()
        self._session: dict[str, str | None] = {}
        self._cached: dict[str, str | None] = {}
        self._persisted: dict[str, str] = {}

    @staticmethod
    def _secure_backend(backend):
        module = type(backend).__module__
        if module == "keyring.backends.chainer":
            for candidate in backend.backends:
                secure = CredentialStore._secure_backend(candidate)
                if secure is not None:
                    return secure
            return None
        supported = {"keyring.backends.SecretService", "keyring.backends.kwallet",
                     "keyring.backends.libsecret", "keyring.backends.macOS",
                     "keyring.backends.OS_X", "keyring.backends.Windows"}
        return backend if module in supported and backend.priority > 0 else None

    def _keyring(self):
        if self._backend is not None:
            return self._backend
        try:
            import keyring
            backend = self._secure_backend(keyring.get_keyring())
            if backend is None:
                raise CredentialError
        except Exception:
            raise CredentialError("No secure desktop keyring is available. Unlock or enable Secret Service or KWallet; passwords can still be used for this session.") from None
        self._backend = backend
        return backend

    def cached(self, calendar_or_url, username: str | None = None) -> str | None:
        """Return an already cached value without accessing the desktop keyring."""
        key = account_key(calendar_or_url, username)
        with self._cache_lock:
            return self._session.get(key, self._cached.get(key))

    def has_session(self, calendar_or_url, username: str | None = None) -> bool:
        key = account_key(calendar_or_url, username)
        with self._cache_lock:
            return bool(self._session.get(key))

    def set_session(self, calendar_or_url, password: str, username: str | None = None) -> None:
        key = account_key(calendar_or_url, username)
        with self._cache_lock:
            self._session[key] = password

    def get(self, calendar_or_url, username: str | None = None) -> str | None:
        key = account_key(calendar_or_url, username)
        with self._cache_lock:
            if key in self._session:
                return self._session[key]
            if key in self._cached:
                return self._cached[key]
        try:
            with self._io_lock:
                with self._cache_lock:
                    if key in self._session:
                        return self._session[key]
                    if key in self._cached:
                        return self._cached[key]
                password = self._keyring().get_password(SERVICE, key)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("Could not read this account password from the desktop keyring. Unlock the keyring or enter the password again.") from None
        with self._cache_lock:
            self._cached[key] = password
            if password is not None:
                self._persisted[key] = password
            return self._session.get(key, password)

    def save(self, calendar_or_url, password: str, username: str | None = None, *, remember: bool = True) -> bool:
        key = account_key(calendar_or_url, username)
        self.set_session(calendar_or_url, password, username)
        if not remember:
            try:
                with self._io_lock:
                    backend = self._keyring()
                    if backend.get_password(SERVICE, key) is not None:
                        backend.delete_password(SERVICE, key)
            except Exception:
                raise CredentialError("This password is available for this session, but a previously saved account password could not be removed. Unlock the desktop keyring and save again with Remember unchecked.") from None
            finally:
                with self._cache_lock:
                    self._cached.pop(key, None)
                    self._persisted.pop(key, None)
            return False
        with self._cache_lock:
            if self._persisted.get(key) == password:
                return True
        try:
            with self._io_lock:
                with self._cache_lock:
                    if self._persisted.get(key) == password:
                        return True
                    self._persisted.pop(key, None)
                self._keyring().set_password(SERVICE, key, password)
        except Exception:
            raise CredentialError("Could not save this account password in the desktop keyring. It is available for this session only. Unlock or enable Secret Service or KWallet and save it again.") from None
        with self._cache_lock:
            self._persisted[key] = password
            self._cached[key] = password
        return True

    def forget(self, calendar_or_url, username: str | None = None) -> None:
        key = account_key(calendar_or_url, username)
        with self._cache_lock:
            self._session[key] = None
            self._cached.pop(key, None)
            self._persisted.pop(key, None)
        try:
            with self._io_lock:
                backend = self._keyring()
                if backend.get_password(SERVICE, key) is not None:
                    backend.delete_password(SERVICE, key)
        except Exception:
            raise CredentialError("Could not remove the saved account password from the desktop keyring. Unlock it and try again.") from None

    def clear_session(self) -> None:
        """Discard in-memory copies without removing securely saved credentials."""
        with self._cache_lock:
            self._session.clear()
            self._cached.clear()
            self._persisted.clear()
