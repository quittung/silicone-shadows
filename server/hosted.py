"""Small persistent and in-memory state for hosted review mode."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CLAIM_IDLE_SECONDS = 3 * 60
CLAIM_MAX_SECONDS = 15 * 60
SESSION_SECONDS = 30 * 24 * 60 * 60
TICKET_SECONDS = 5 * 60
TICKET_HEARTBEAT_SECONDS = 30
UPLOAD_RESERVATION_SECONDS = 30
RATE_SECONDS = 10 * 60
RATE_COUNT = 5
QUEUE_LIMIT = 20
ARCHIVE_TOKEN_SECONDS = 60 * 60


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class User:
    id: int
    name: str
    reviewer: bool


class ClaimError(Exception):
    pass


class HostedStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    reviewer INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invites (
                    token_hash TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    reviewer INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    item_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    claimed_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    item_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    kind TEXT NOT NULL DEFAULT 'catalog',
                    source TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(submissions)")
            }
            if "kind" not in columns:
                db.execute(
                    "ALTER TABLE submissions ADD COLUMN kind TEXT NOT NULL DEFAULT 'catalog'"
                )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    def create_invite(
        self, name: str, reviewer: bool = False, lifetime: float = 7 * 24 * 60 * 60
    ) -> str:
        name = name.strip()
        if not name:
            raise ValueError("invite name must not be empty")
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute(
                "DELETE FROM invites WHERE expires_at <= ?",
                (time.time(),),
            )
            db.execute(
                "INSERT INTO invites VALUES (?, ?, ?, ?)",
                (token_hash(token), name, reviewer, time.time() + lifetime),
            )
        return token

    def redeem_invite(self, token: str) -> tuple[str, User] | None:
        now = time.time()
        session = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM invites WHERE expires_at <= ?",
                (now,),
            )
            invite = db.execute(
                "SELECT * FROM invites WHERE token_hash = ?",
                (token_hash(token),),
            ).fetchone()
            if not invite or invite["expires_at"] <= now:
                return None
            user_id = db.execute(
                "INSERT INTO users(name, reviewer, created_at) VALUES (?, ?, ?)",
                (invite["name"], invite["reviewer"], now),
            ).lastrowid
            db.execute(
                "DELETE FROM invites WHERE token_hash = ?", (invite["token_hash"],)
            )
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (token_hash(session), user_id, now + SESSION_SECONDS),
            )
        return session, User(user_id, invite["name"], bool(invite["reviewer"]))

    def user_for_session(self, token: str | None) -> User | None:
        if not token:
            return None
        now = time.time()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT users.id, users.name, users.reviewer
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (token_hash(token), now),
            ).fetchone()
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        return User(row["id"], row["name"], bool(row["reviewer"])) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self.connect() as db:
                db.execute(
                    "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)
                )

    def revoke_sessions(self, name: str) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM sessions WHERE user_id IN "
                "(SELECT id FROM users WHERE name = ?)",
                (name,),
            )
        return cursor.rowcount

    @staticmethod
    def claim_is_live(row: sqlite3.Row, now: float) -> bool:
        return (
            now - row["heartbeat_at"] < CLAIM_IDLE_SECONDS
            and now - row["claimed_at"] < CLAIM_MAX_SECONDS
        )

    def acquire_claim(
        self,
        item_id: str,
        user: User,
        discard: Callable[[str], None] | None = None,
    ) -> tuple[list[str], float]:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute(
                "SELECT claims.*, users.name FROM claims JOIN users ON users.id = claims.user_id WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            expired = []
            if target and not self.claim_is_live(target, now):
                if discard:
                    discard(item_id)
                db.execute("DELETE FROM claims WHERE item_id = ?", (item_id,))
                target = None
                expired.append(item_id)
            if target and target["user_id"] != user.id:
                raise ClaimError(f"{target['name']} is already working on this product")
            other = db.execute(
                "SELECT * FROM claims WHERE user_id = ? AND item_id != ?",
                (user.id, item_id),
            ).fetchone()
            if other and self.claim_is_live(other, now):
                raise ClaimError("release the current product before opening another")
            if target:
                db.execute(
                    "UPDATE claims SET heartbeat_at = ? WHERE item_id = ?",
                    (now, item_id),
                )
                claimed_at = target["claimed_at"]
            else:
                db.execute(
                    "INSERT INTO claims VALUES (?, ?, ?, ?)",
                    (item_id, user.id, now, now),
                )
                claimed_at = now
        return expired, claimed_at + CLAIM_MAX_SECONDS

    def heartbeat(
        self,
        item_id: str,
        user: User,
        discard: Callable[[str], None] | None = None,
    ) -> float:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM claims WHERE item_id = ?", (item_id,)
            ).fetchone()
            if not row or row["user_id"] != user.id:
                raise ClaimError("this product is not claimed by your session")
            if not self.claim_is_live(row, now):
                if discard:
                    discard(item_id)
                db.execute("DELETE FROM claims WHERE item_id = ?", (item_id,))
                raise ClaimError("the claim expired; unfinished work was discarded")
            db.execute(
                "UPDATE claims SET heartbeat_at = ? WHERE item_id = ?",
                (now, item_id),
            )
            return row["claimed_at"] + CLAIM_MAX_SECONDS

    def require_claim(
        self,
        item_id: str,
        user: User,
        discard: Callable[[str], None] | None = None,
    ) -> None:
        now = time.time()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM claims WHERE item_id = ?", (item_id,)
            ).fetchone()
            if not row or row["user_id"] != user.id or not self.claim_is_live(row, now):
                if row and not self.claim_is_live(row, now):
                    if discard:
                        discard(item_id)
                    db.execute("DELETE FROM claims WHERE item_id = ?", (item_id,))
                raise ClaimError("this product is not claimed by your session")

    def release_claims(
        self,
        user: User,
        item_id: str | None = None,
        discard: Callable[[str], None] | None = None,
    ) -> list[str]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if item_id is None:
                rows = db.execute(
                    "SELECT item_id FROM claims WHERE user_id = ?", (user.id,)
                ).fetchall()
                db.execute("DELETE FROM claims WHERE user_id = ?", (user.id,))
            else:
                rows = db.execute(
                    "SELECT item_id FROM claims WHERE user_id = ? AND item_id = ?",
                    (user.id, item_id),
                ).fetchall()
                db.execute(
                    "DELETE FROM claims WHERE user_id = ? AND item_id = ?",
                    (user.id, item_id),
                )
            if discard:
                for row in rows:
                    discard(row["item_id"])
        return [row["item_id"] for row in rows]

    def claims(self) -> dict[str, dict]:
        now = time.time()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT claims.*, users.name FROM claims
                JOIN users ON users.id = claims.user_id
                """
            ).fetchall()
            expired = [
                row["item_id"] for row in rows if not self.claim_is_live(row, now)
            ]
        return {
            row["item_id"]: {
                "user_id": row["user_id"],
                "name": row["name"],
                "expires_at": min(
                    row["heartbeat_at"] + CLAIM_IDLE_SECONDS,
                    row["claimed_at"] + CLAIM_MAX_SECONDS,
                ),
            }
            for row in rows
            if row["item_id"] not in expired
        }

    def put_submission(
        self,
        item_id: str,
        user: User,
        source: str,
        state_json: str,
        kind: str = "catalog",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO submissions(item_id, user_id, kind, source, state_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item_id, user.id, kind, source, state_json, time.time()),
            )
            db.execute("DELETE FROM claims WHERE item_id = ?", (item_id,))

    def submission(self, item_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                """
                SELECT submissions.*, users.name AS user_name
                FROM submissions JOIN users ON users.id = submissions.user_id
                WHERE item_id = ?
                """,
                (item_id,),
            ).fetchone()

    def submissions(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                """
                SELECT submissions.*, users.name AS user_name
                FROM submissions JOIN users ON users.id = submissions.user_id
                ORDER BY submissions.created_at
                """
            ).fetchall()

    def remove_submission(self, item_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM submissions WHERE item_id = ?", (item_id,))


class QueueError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class PublicQueue:
    def __init__(self):
        self.lock = threading.Lock()
        self.secret = secrets.token_bytes(32)
        self.tickets: dict[str, dict] = {}
        self.order: list[str] = []
        self.active: str | None = None
        self.rates: dict[str, deque[float]] = {}
        self.archive_tokens: dict[str, dict] = {}

    def address_key(self, address: str) -> str:
        try:
            value = ipaddress.ip_address(address)
            normalized = (
                str(ipaddress.ip_network(f"{value}/64", strict=False))
                if value.version == 6
                else str(value)
            )
        except ValueError:
            normalized = "unknown"
        return hmac.new(self.secret, normalized.encode(), hashlib.sha256).hexdigest()

    def _cleanup(self, now: float) -> None:
        for key, timestamps in list(self.rates.items()):
            while timestamps and now - timestamps[0] >= RATE_SECONDS:
                timestamps.popleft()
            if not timestamps:
                del self.rates[key]
        for token, record in list(self.archive_tokens.items()):
            if record["expires_at"] <= now:
                del self.archive_tokens[token]
        expired = []
        for token, ticket in self.tickets.items():
            stale = (
                ticket["status"] != "processing"
                and now - ticket["last_seen"] >= TICKET_HEARTBEAT_SECONDS
            )
            old = now - ticket["created_at"] >= TICKET_SECONDS
            abandoned = (
                token == self.active
                and ticket["status"] == "ready"
                and now - ticket["reserved_at"] >= UPLOAD_RESERVATION_SECONDS
            )
            if stale or old or abandoned:
                expired.append(token)
        for token in expired:
            self.tickets.pop(token, None)
            if token in self.order:
                self.order.remove(token)
            if self.active == token:
                self.active = None

    def create(self, address: str) -> dict:
        now = time.time()
        key = self.address_key(address)
        with self.lock:
            self._cleanup(now)
            existing = next(
                (ticket for ticket in self.tickets.values() if ticket["key"] == key),
                None,
            )
            if existing:
                existing["last_seen"] = now
                return self._response(existing)
            timestamps = self.rates.setdefault(key, deque())
            if len(timestamps) >= RATE_COUNT:
                raise QueueError("rate limit reached; try again later", 429)
            if len(self.order) >= QUEUE_LIMIT:
                raise QueueError("the processing queue is full", 503)
            token = secrets.token_urlsafe(24)
            ticket = {
                "token": token,
                "key": key,
                "created_at": now,
                "last_seen": now,
                "status": "queued",
                "reserved_at": None,
            }
            self.tickets[token] = ticket
            self.order.append(token)
            timestamps.append(now)
            return self._response(ticket)

    def status(self, token: str, address: str) -> dict:
        now = time.time()
        key = self.address_key(address)
        with self.lock:
            self._cleanup(now)
            ticket = self.tickets.get(token)
            if not ticket or ticket["key"] != key:
                raise QueueError("queue ticket expired", 404)
            ticket["last_seen"] = now
            if self.active is None and self.order and self.order[0] == token:
                self.active = token
                ticket["status"] = "ready"
                ticket["reserved_at"] = now
            return self._response(ticket)

    def begin(self, token: str, address: str) -> None:
        now = time.time()
        key = self.address_key(address)
        with self.lock:
            self._cleanup(now)
            ticket = self.tickets.get(token)
            if (
                not ticket
                or ticket["key"] != key
                or self.active != token
                or ticket["status"] != "ready"
            ):
                raise QueueError("queue ticket is not ready", 409)
            ticket["status"] = "processing"
            ticket["last_seen"] = now

    def finish(self, token: str, address: str | None = None) -> str | None:
        with self.lock:
            self.tickets.pop(token, None)
            if token in self.order:
                self.order.remove(token)
            if self.active == token:
                self.active = None
            if address is None:
                return None
            archive_token = secrets.token_urlsafe(24)
            self.archive_tokens[archive_token] = {
                "key": self.address_key(address),
                "expires_at": time.time() + ARCHIVE_TOKEN_SECONDS,
            }
            return archive_token

    def consume_archive(self, token: str, address: str) -> None:
        now = time.time()
        key = self.address_key(address)
        with self.lock:
            self._cleanup(now)
            record = self.archive_tokens.pop(token, None)
            if not record or record["key"] != key:
                raise QueueError("background-removal proof expired", 403)

    def _response(self, ticket: dict) -> dict:
        return {
            "ticket": ticket["token"],
            "status": ticket["status"],
            "position": self.order.index(ticket["token"]) + 1,
            "expires_in": max(
                0, round(TICKET_SECONDS - (time.time() - ticket["created_at"]))
            ),
        }
