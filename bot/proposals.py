"""SQLite persistence for bounded conversations and reviewed directory proposals."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bot.directory import DirectoryConflict, commit_change


class ProposalStore:
    """Durable proposal state. Duplicate webhook deliveries are intentionally not claimed."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                    group_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    prepared_json TEXT NOT NULL, business_ids_json TEXT NOT NULL,
                    proposal_text TEXT NOT NULL, source_message_id TEXT,
                    message_id TEXT, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(request_id, revision)
                );
                CREATE TABLE IF NOT EXISTS proposal_places (
                    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
                    business_id TEXT NOT NULL,
                    PRIMARY KEY(proposal_id, business_id)
                );
                CREATE INDEX IF NOT EXISTS proposal_places_business ON proposal_places(business_id);
                CREATE INDEX IF NOT EXISTS proposals_group_message ON proposals(group_id, message_id);
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL, request_id TEXT, event_id TEXT, message_id TEXT,
                    proposal_id INTEGER, role TEXT NOT NULL,
                    text TEXT NOT NULL, timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_scope ON turns(group_id, sender_id, request_id, id);
                CREATE INDEX IF NOT EXISTS turns_message ON turns(group_id, message_id);
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER NOT NULL,
                    attempted_at TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT,
                    detail TEXT
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _stamp(value: datetime | None = None) -> str:
        value = value or datetime.now(UTC)
        if value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    def record_user_turn(self, group_id: str, sender_id: str, event_id: str | None,
                         text: str, timestamp: datetime, max_turns: int,
                         request_id: str | None = None) -> list[dict[str, str]]:
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        stamp = self._stamp(timestamp)
        with self._connect() as conn:
            conn.execute("INSERT INTO turns(group_id,sender_id,request_id,event_id,role,text,timestamp) VALUES(?,?,?,?,?,?,?)",
                         (group_id, sender_id, request_id, event_id, "user", text, stamp))
            rows = conn.execute("SELECT role,text,timestamp FROM turns WHERE group_id=? AND sender_id=? AND request_id IS ? ORDER BY id DESC LIMIT ?",
                                (group_id, sender_id, request_id, max_turns * 2)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_assistant_turn(self, group_id: str, sender_id: str, text: str,
                              timestamp: datetime | None = None, request_id: str | None = None,
                              message_id: str | None = None, proposal_id: int | None = None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO turns(group_id,sender_id,request_id,message_id,proposal_id,role,text,timestamp) VALUES(?,?,?,?,?,?,?,?)",
                         (group_id, sender_id, request_id, message_id, proposal_id, "assistant", text, self._stamp(timestamp)))

    def request_for_message(self, group_id: str, message_id: str) -> str | None:
        """Resolve a reply to a stored assistant clarification/proposal to its task."""
        with self._connect() as conn:
            row = conn.execute("SELECT request_id FROM turns WHERE group_id=? AND message_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                               (group_id, message_id)).fetchone()
        return row[0] if row else None

    def proposal_for_turn_message(self, group_id: str, message_id: str) -> dict[str, Any] | None:
        """Return the proposal revision that was active when an assistant turn was sent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.* FROM turns t JOIN proposals p ON p.id=t.proposal_id "
                "WHERE t.group_id=? AND t.message_id=? AND t.role='assistant' "
                "ORDER BY t.id DESC LIMIT 1", (group_id, message_id),
            ).fetchone()
        return self._proposal_dict(row) if row else None

    def cancel_proposal(self, group_id: str, message_id: str) -> dict[str, Any] | None:
        """Cancel exactly the active proposal represented by this WhatsApp message."""
        now = self._stamp()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM proposals WHERE group_id=? AND message_id=? AND status='pending' "
                "ORDER BY id DESC LIMIT 1", (group_id, message_id),
            ).fetchone()
            if not row:
                return None
            proposal = self._proposal_dict(row)
            conn.execute("UPDATE proposals SET status='cancelled',updated_at=? WHERE id=? AND status='pending'",
                         (now, proposal["id"]))
            conn.execute(
                "INSERT INTO delivery_attempts(proposal_id,attempted_at,status,message_id,detail) "
                "VALUES(?,?,'cancelled',?,'operator withdrew the complete proposal')",
                (proposal["id"], now, message_id),
            )
            return {**proposal, "status": "cancelled"}

    def conversation_turns(self, group_id: str, sender_id: str, max_turns: int,
                           request_id: str | None = None) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT role,text,timestamp FROM turns WHERE group_id=? AND sender_id=? AND request_id IS ? ORDER BY id DESC LIMIT ?",
                                (group_id, sender_id, request_id, max_turns * 2)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def active_proposals_for(self, group_id: str, place_ids: list[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT DISTINCT p.* FROM proposals p WHERE p.group_id=? AND p.status='pending'"
        args: list[Any] = [group_id]
        if place_ids:
            marks = ",".join("?" for _ in place_ids)
            query += f" AND p.id IN (SELECT proposal_id FROM proposal_places WHERE business_id IN ({marks}))"
            args.extend(place_ids)
        query += " ORDER BY p.id"
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [self._proposal_dict(row) for row in rows]

    def revision_count(self, request_id: str) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM proposals WHERE request_id=?", (request_id,)).fetchone()[0])

    def active_revision_count(self, group_id: str, business_ids: list[str]) -> int:
        """Return the highest active revision count among proposals touching these IDs."""
        if not business_ids:
            return 0
        marks = ",".join("?" for _ in business_ids)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COALESCE(MAX(p.revision),0) FROM proposals p JOIN proposal_places b ON b.proposal_id=p.id WHERE p.group_id=? AND p.status='pending' AND b.business_id IN ({marks})",
                               [group_id, *business_ids]).fetchone()
        return int(row[0])

    @staticmethod
    def _proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "request_id": row["request_id"], "group_id": row["group_id"],
                "revision": row["revision"], "prepared": json.loads(row["prepared_json"]),
                "business_ids": json.loads(row["business_ids_json"]), "proposal_text": row["proposal_text"],
                "source_message_id": row["source_message_id"], "message_id": row["message_id"],
                "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def create_proposal(self, group_id: str, request_id: str, prepared_change: dict[str, Any],
                        business_ids: list[str], proposal_text: str,
                        source_message_id: str | None = None) -> tuple[int, list[str]]:
        if not business_ids or len(set(business_ids)) != len(business_ids):
            raise ValueError("business_ids must be a non-empty unique list")
        now = self._stamp()
        stale_ids: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO requests(id,group_id,sender_id,created_at) VALUES(?,?,?,?)",
                         (request_id, group_id, "", now))
            previous = conn.execute("SELECT MAX(revision) FROM proposals WHERE request_id=?", (request_id,)).fetchone()[0]
            revision = (previous or 0) + 1
            marks = ",".join("?" for _ in business_ids)
            overlaps = conn.execute(f"SELECT DISTINCT p.id,p.message_id FROM proposals p JOIN proposal_places b ON b.proposal_id=p.id WHERE p.group_id=? AND p.status='pending' AND b.business_id IN ({marks})",
                                    [group_id, *business_ids]).fetchall()
            for row in overlaps:
                if row["message_id"]:
                    stale_ids.append(row["message_id"])
                conn.execute("UPDATE proposals SET status='superseded',updated_at=? WHERE id=?", (now, row["id"]))
            cur = conn.execute("INSERT INTO proposals(request_id,group_id,revision,prepared_json,business_ids_json,proposal_text,source_message_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                               (request_id, group_id, revision, json.dumps(prepared_change, ensure_ascii=False),
                                json.dumps(business_ids, ensure_ascii=False), proposal_text, source_message_id, now, now))
            proposal_id = int(cur.lastrowid)
            conn.executemany("INSERT INTO proposal_places(proposal_id,business_id) VALUES(?,?)",
                             [(proposal_id, place_id) for place_id in business_ids])
            conn.execute("INSERT INTO delivery_attempts(proposal_id,attempted_at,status) VALUES(?,?,'prepared')", (proposal_id, now))
        return proposal_id, stale_ids

    def record_delivery(self, proposal_id: int, status: str, message_id: str | None = None, detail: str | None = None) -> None:
        if status not in {"sent", "failed", "delivery-unknown"}:
            raise ValueError("invalid delivery status")
        now = self._stamp()
        with self._connect() as conn:
            conn.execute("INSERT INTO delivery_attempts(proposal_id,attempted_at,status,message_id,detail) VALUES(?,?,?,?,?)",
                         (proposal_id, now, status, message_id, detail))
            conn.execute("UPDATE proposals SET message_id=COALESCE(?,message_id),updated_at=? WHERE id=?",
                         (message_id, now, proposal_id))
            if status == "delivery-unknown":
                conn.execute("UPDATE proposals SET status='delivery-unknown' WHERE id=? AND status='pending'", (proposal_id,))
            elif status == "failed":
                conn.execute("UPDATE proposals SET status='failed' WHERE id=? AND status='pending'", (proposal_id,))

    def get_active_proposal(self, group_id: str, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE group_id=? AND message_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                               (group_id, message_id)).fetchone()
        return self._proposal_dict(row) if row else None

    def proposal_for_message(self, group_id: str, message_id: str) -> dict[str, Any] | None:
        """Look up a proposal message regardless of lifecycle state."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE group_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
                               (group_id, message_id)).fetchone()
        return self._proposal_dict(row) if row else None

    def approve(self, group_id: str, message_id: str, places_path: str | Path) -> dict[str, Any] | None:
        """Commit only if the message still represents the active set for every business."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM proposals WHERE group_id=? AND message_id=? AND status='pending'", (group_id, message_id)).fetchone()
            if not row:
                return None
            proposal = self._proposal_dict(row)
            for business_id in proposal["business_ids"]:
                active = conn.execute("SELECT p.id FROM proposals p JOIN proposal_places b ON b.proposal_id=p.id WHERE p.group_id=? AND p.status='pending' AND b.business_id=?", (group_id, business_id)).fetchall()
                if len(active) != 1 or active[0]["id"] != proposal["id"]:
                    conn.execute("UPDATE proposals SET status='superseded',updated_at=? WHERE id=?", (self._stamp(), proposal["id"]))
                    return {**proposal, "status": "superseded"}
            try:
                commit_change(places_path, proposal["prepared"])
            except DirectoryConflict as exc:
                conn.execute("UPDATE proposals SET status='conflict',updated_at=? WHERE id=?", (self._stamp(), proposal["id"]))
                return {**proposal, "status": "conflict", "error": str(exc)}
            except Exception:
                conn.execute("UPDATE proposals SET status='failed',updated_at=? WHERE id=?", (self._stamp(), proposal["id"]))
                failure = "directory commit failed"
            else:
                failure = None
            if failure:
                return {**proposal, "status": "failed", "error": failure}
            conn.execute("UPDATE proposals SET status='committed',updated_at=? WHERE id=?", (self._stamp(), proposal["id"]))
            return {**proposal, "status": "committed"}

