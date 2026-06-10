import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import List, Optional
from uuid import uuid4

from backend.config.settings import get_settings


class ApprovalStore:
    """SQLite-backed approval store for action review workflow."""

    def __init__(self, db_path: str | None = None):
        settings = get_settings()
        self.db_path = Path(db_path or settings.approval_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    action TEXT NOT NULL,
                    command TEXT,
                    target TEXT,
                    risk TEXT NOT NULL,
                    source_query TEXT,
                    requested_by TEXT,
                    reviewer TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create(self, action: str, command: Optional[str], target: Optional[str], risk: str, source_query: Optional[str], requested_by: Optional[str]) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        approval_id = str(uuid4())
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO approvals (
                        id, status, action, command, target, risk,
                        source_query, requested_by, reviewer, review_note,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_id,
                        "pending",
                        action,
                        command,
                        target,
                        risk,
                        source_query,
                        requested_by,
                        None,
                        None,
                        now,
                        now,
                    ),
                )
        return self.get(approval_id) or {}

    def list(self, status: Optional[str] = None) -> list[dict]:
        with self._lock:
            query = "SELECT * FROM approvals"
            params: tuple = ()
            if status:
                query += " WHERE status = ?"
                params = (status,)
            query += " ORDER BY created_at DESC"
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get(self, approval_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def decide(self, approval_id: str, decision: str, reviewer: Optional[str], note: Optional[str]) -> Optional[dict]:
        if decision not in {"approved", "rejected"}:
            return None

        with self._lock:
            current = self.get(approval_id)
            if not current:
                return None
            if current.get("status") != "pending":
                return current

            updated_at = datetime.now(timezone.utc).isoformat()
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE approvals
                    SET status = ?, reviewer = ?, review_note = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (decision, reviewer, note, updated_at, approval_id),
                )
            return self.get(approval_id)

    def mark_executed(self, approval_id: str, note: Optional[str] = None) -> Optional[dict]:
        """Mark an approved item as executed so it cannot be re-run from the UI."""
        with self._lock:
            current = self.get(approval_id)
            if not current:
                return None
            updated_at = datetime.now(timezone.utc).isoformat()
            with self._conn:
                self._conn.execute(
                    "UPDATE approvals SET status = 'executed', review_note = COALESCE(?, review_note), updated_at = ? WHERE id = ?",
                    (note, updated_at, approval_id),
                )
            return self.get(approval_id)

    def delete(self, approval_id: str) -> bool:
        with self._lock:
            with self._conn:
                cur = self._conn.execute("DELETE FROM approvals WHERE id = ?", (approval_id,))
        return cur.rowcount > 0

    # NB: `List` from typing because the `list` builtin is shadowed by the
    # `list()` method above within this class body.
    def cleanup(self, remove_empty: bool = True, action: Optional[str] = None, statuses: Optional[List[str]] = None) -> int:
        """Delete approvals matching the given filters. Returns the number deleted."""
        clauses: list[str] = []
        params: list = []
        if remove_empty:
            clauses.append("(command IS NULL OR trim(command) = '')")
        if action:
            clauses.append("action = ?")
            params.append(action)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if not clauses:
            return 0
        query = "DELETE FROM approvals WHERE " + " OR ".join(clauses)
        with self._lock:
            with self._conn:
                cur = self._conn.execute(query, params)
        return cur.rowcount

    @staticmethod
    def _row_to_item(row: sqlite3.Row | None) -> Optional[dict]:
        if row is None:
            return None
        return {
            "id": row["id"],
            "status": row["status"],
            "action": row["action"],
            "command": row["command"],
            "target": row["target"],
            "risk": row["risk"],
            "source_query": row["source_query"],
            "requested_by": row["requested_by"],
            "reviewer": row["reviewer"],
            "review_note": row["review_note"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
        }
