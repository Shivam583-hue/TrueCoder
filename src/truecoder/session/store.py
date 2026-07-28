from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from truecoder.agent.messages import ModelMessage
from truecoder.session.codec import decode_turn, encode_turn
from truecoder.session.models import (
    SessionFormatError,
    SessionNotFoundError,
    SessionRecord,
    SessionStorageError,
    SessionSummary,
)

DATABASE_VERSION = 1
DEFAULT_SESSION_TITLE = "New session"


class SQLiteSessionStore:
    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a pathlib.Path")

        self.database_path = database_path.expanduser().resolve()
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.database_path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._initialize_schema()
        except sqlite3.Error as error:
            raise SessionStorageError(
                f"Could not open session database: {error}"
            ) from error

    def _initialize_schema(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, DATABASE_VERSION}:
            raise SessionStorageError(
                f"Unsupported session database version: {version}"
            )
        if version == DATABASE_VERSION:
            return

        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    project_root TEXT NOT NULL,
                    title TEXT NOT NULL,
                    title_is_custom INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE turns (
                    session_id TEXT NOT NULL
                        REFERENCES sessions(session_id)
                        ON DELETE CASCADE,
                    turn_index INTEGER NOT NULL,
                    messages_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, turn_index)
                );
                CREATE INDEX sessions_project_updated
                    ON sessions(project_root, updated_at DESC);
                PRAGMA user_version = 1;
                """
            )

    @staticmethod
    def _project_key(project_root: Path) -> str:
        if not isinstance(project_root, Path):
            raise TypeError("project_root must be a pathlib.Path")
        return str(project_root.resolve(strict=True))

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def create_session(self, project_root: Path) -> SessionSummary:
        project_key = self._project_key(project_root)
        session_id = uuid4().hex
        now = self._now().isoformat()
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, project_root, title, title_is_custom,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 0, ?, ?)
                    """,
                    (session_id, project_key, DEFAULT_SESSION_TITLE, now, now),
                )
        except sqlite3.Error as error:
            raise SessionStorageError(f"Could not create session: {error}") from error
        return self._get_summary(project_key, session_id)

    def list_sessions(self, project_root: Path) -> tuple[SessionSummary, ...]:
        project_key = self._project_key(project_root)
        try:
            rows = self._connection.execute(
                """
                SELECT sessions.*, COUNT(turns.turn_index) AS turn_count
                FROM sessions
                LEFT JOIN turns USING (session_id)
                WHERE project_root = ?
                GROUP BY sessions.session_id
                ORDER BY updated_at DESC, created_at DESC
                """,
                (project_key,),
            ).fetchall()
        except sqlite3.Error as error:
            raise SessionStorageError(f"Could not list sessions: {error}") from error
        return tuple(self._summary_from_row(row) for row in rows)

    def load_session(
        self,
        project_root: Path,
        session_id: str,
    ) -> SessionRecord:
        project_key = self._project_key(project_root)
        summary = self._get_summary(project_key, session_id)
        try:
            rows = self._connection.execute(
                """
                SELECT messages_json
                FROM turns
                WHERE session_id = ?
                ORDER BY turn_index ASC
                """,
                (session_id,),
            ).fetchall()
            turns = tuple(tuple(decode_turn(row["messages_json"])) for row in rows)
        except sqlite3.Error as error:
            raise SessionStorageError(f"Could not load session: {error}") from error
        except SessionFormatError:
            raise
        return SessionRecord(summary=summary, completed_turns=turns)

    def save_completed_turns(
        self,
        project_root: Path,
        session_id: str,
        turns: Sequence[Sequence[ModelMessage]],
    ) -> SessionSummary:
        project_key = self._project_key(project_root)
        encoded_turns = tuple(encode_turn(turn) for turn in turns)
        current = self._get_summary(project_key, session_id)
        if current.turn_count > len(encoded_turns):
            raise SessionStorageError(
                "Stored session contains more turns than active state."
            )
        if current.turn_count == len(encoded_turns):
            return current

        now = self._now().isoformat()
        try:
            with self._connection:
                self._connection.executemany(
                    """
                    INSERT INTO turns (session_id, turn_index, messages_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (session_id, index, encoded_turns[index])
                        for index in range(current.turn_count, len(encoded_turns))
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE sessions SET updated_at = ?
                    WHERE session_id = ? AND project_root = ?
                    """,
                    (now, session_id, project_key),
                )
        except sqlite3.Error as error:
            raise SessionStorageError(f"Could not save session: {error}") from error
        return self._get_summary(project_key, session_id)

    def rename_session(
        self,
        project_root: Path,
        session_id: str,
        title: str,
        *,
        custom: bool = True,
    ) -> SessionSummary:
        project_key = self._project_key(project_root)
        self._get_summary(project_key, session_id)
        now = self._now().isoformat()
        try:
            with self._connection:
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET title = ?, title_is_custom = ?, updated_at = ?
                    WHERE session_id = ? AND project_root = ?
                    """,
                    (title, int(custom), now, session_id, project_key),
                )
        except sqlite3.Error as error:
            raise SessionStorageError(f"Could not rename session: {error}") from error
        return self._get_summary(project_key, session_id)

    def delete_session(self, project_root: Path, session_id: str) -> None:
        project_key = self._project_key(project_root)
        self._get_summary(project_key, session_id)
        try:
            with self._connection:
                self._connection.execute(
                    "DELETE FROM sessions WHERE session_id = ? AND project_root = ?",
                    (session_id, project_key),
                )
        except sqlite3.Error as error:
            raise SessionStorageError(f"Could not delete session: {error}") from error

    def _get_summary(self, project_key: str, session_id: str) -> SessionSummary:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id cannot be empty")
        try:
            row = self._connection.execute(
                """
                SELECT sessions.*, COUNT(turns.turn_index) AS turn_count
                FROM sessions
                LEFT JOIN turns USING (session_id)
                WHERE session_id = ? AND project_root = ?
                GROUP BY sessions.session_id
                """,
                (session_id.strip(), project_key),
            ).fetchone()
        except sqlite3.Error as error:
            raise SessionStorageError(f"Could not load session: {error}") from error
        if row is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        return self._summary_from_row(row)

    @staticmethod
    def _summary_from_row(row: sqlite3.Row) -> SessionSummary:
        try:
            created_at = datetime.fromisoformat(row["created_at"])
            updated_at = datetime.fromisoformat(row["updated_at"])
            project_root = Path(row["project_root"])
            if created_at.tzinfo is None or updated_at.tzinfo is None:
                raise ValueError("timestamps must include a timezone")
            return SessionSummary(
                session_id=row["session_id"],
                project_root=project_root,
                title=row["title"],
                created_at=created_at,
                updated_at=updated_at,
                turn_count=int(row["turn_count"]),
                title_is_custom=bool(row["title_is_custom"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SessionFormatError(f"Invalid session metadata: {error}") from error

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as error:
            raise SessionStorageError(
                f"Could not close session database: {error}"
            ) from error
