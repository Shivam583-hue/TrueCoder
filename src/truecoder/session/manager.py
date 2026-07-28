from __future__ import annotations

import re
from pathlib import Path

from truecoder.agent.state import AgentState
from truecoder.session.models import SessionRecord, SessionSummary
from truecoder.session.store import SQLiteSessionStore

MAX_AUTOMATIC_TITLE_LENGTH = 72
MAX_SESSION_TITLE_LENGTH = 120


def normalize_title(title: str, *, max_length: int = MAX_SESSION_TITLE_LENGTH) -> str:
    if not isinstance(title, str):
        raise TypeError("title must be a string")
    normalized = re.sub(r"\s+", " ", title).strip()
    if not normalized:
        raise ValueError("Session title cannot be empty.")
    return normalized[:max_length]


class SessionManager:
    def __init__(
        self,
        store: SQLiteSessionStore,
        state: AgentState,
        project_root: Path,
    ) -> None:
        self.store = store
        self.state = state
        self.project_root = project_root.resolve(strict=True)
        self._active_session = self.store.create_session(self.project_root)
        self._closed = False

    @property
    def active_session(self) -> SessionSummary:
        return self._active_session

    def create_session(self) -> SessionSummary:
        previous_session = self._active_session
        new_session = self.store.create_session(self.project_root)
        self.state.reset()
        self._active_session = new_session
        self._delete_if_empty(previous_session)
        return self._active_session

    def save_completed_turns(self) -> SessionSummary:
        summary = self.store.save_completed_turns(
            self.project_root,
            self._active_session.session_id,
            self.state.completed_turns,
        )
        if (
            summary.turn_count
            and not summary.title_is_custom
            and summary.title == "New session"
        ):
            first_message = self.state.completed_turns[0][0]
            title = normalize_title(
                first_message["content"],
                max_length=MAX_AUTOMATIC_TITLE_LENGTH,
            )
            summary = self.store.rename_session(
                self.project_root,
                summary.session_id,
                title,
                custom=False,
            )
        self._active_session = summary
        return summary

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        return self.store.list_sessions(self.project_root)

    def switch_session(self, session_id: str) -> SessionRecord:
        previous_session = self._active_session
        record = self.store.load_session(self.project_root, session_id)
        self.state.replace_completed_turns(record.completed_turns)
        self._active_session = record.summary
        if previous_session.session_id != record.summary.session_id:
            self._delete_if_empty(previous_session)
        return record

    def rename_session(self, session_id: str, title: str) -> SessionSummary:
        normalized = normalize_title(title)
        summary = self.store.rename_session(
            self.project_root,
            session_id,
            normalized,
        )
        if session_id == self._active_session.session_id:
            self._active_session = summary
        return summary

    def delete_session(self, session_id: str) -> None:
        deleting_active = session_id == self._active_session.session_id
        self.store.delete_session(self.project_root, session_id)
        if deleting_active:
            self.state.reset()
            self._active_session = self.store.create_session(self.project_root)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._delete_if_empty(self._active_session)
        finally:
            self.store.close()
            self._closed = True

    def _delete_if_empty(self, session: SessionSummary) -> None:
        if session.turn_count == 0:
            self.store.delete_session(self.project_root, session.session_id)
