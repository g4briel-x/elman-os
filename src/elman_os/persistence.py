"""SQLite persistence for local ELMAN-OS workflow and approval evidence."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from .approvals import ApprovalRecord, ApprovalStatus
from .domain import WorkflowReport


class SQLiteKernelStore:
    """Small local store; production deployments can replace it by an adapter."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if os.name != "nt":
            self.database_path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open one transaction and always release its Windows file handle."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS workflow_reports (
                    workflow_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stop_reason TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    request_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def save_workflow(self, report: WorkflowReport) -> None:
        payload = json.dumps(report.to_dict(), ensure_ascii=False, default=str)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO workflow_reports (
                    workflow_id, status, stop_reason, report_json, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    status = excluded.status,
                    stop_reason = excluded.stop_reason,
                    report_json = excluded.report_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    report.workflow_id,
                    report.status.value,
                    report.stop_reason.value,
                    payload,
                ),
            )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT report_json FROM workflow_reports WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        return None if row is None else json.loads(row["report_json"])

    def list_workflows(self, limit: int = 50) -> list[dict[str, str]]:
        if not 1 <= limit <= 500:
            raise ValueError("La limite doit être comprise entre 1 et 500")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT workflow_id, status, stop_reason, updated_at
                FROM workflow_reports
                ORDER BY updated_at DESC, workflow_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_approval(self, record: ApprovalRecord) -> None:
        payload = json.dumps(asdict(record), ensure_ascii=False, default=str)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    request_id, action, status, record_json, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(request_id) DO UPDATE SET
                    action = excluded.action,
                    status = excluded.status,
                    record_json = excluded.record_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (record.request_id, record.action, record.status.value, payload),
            )

    def get_approval(self, request_id: str) -> ApprovalRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM approvals WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["record_json"])
        data["status"] = ApprovalStatus(data["status"])
        return ApprovalRecord(**data)
