"""Read-only workflow history adapter for ELMAN Studio."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote


class HistoryReadError(RuntimeError):
    """The Studio history database or one of its reports is unreadable."""


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    workflow_id: str
    status: str
    stop_reason: str
    updated_at: str
    iteration_count: int
    final_verdict: str | None


@dataclass(frozen=True, slots=True)
class WorkflowDetails:
    summary: WorkflowSnapshot
    evidence: tuple[str, ...]
    decisions: tuple[str, ...]
    learning_proposals: tuple[str, ...]
    memory_keys: tuple[str, ...]


class WorkflowHistoryReader:
    """Read workflow reports without creating or mutating the SQLite database."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    @property
    def available(self) -> bool:
        return self.database_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        if not self.available:
            raise FileNotFoundError(self.database_path)

        encoded_path = quote(self.database_path.as_posix(), safe="/:")
        try:
            connection = sqlite3.connect(
                f"file:{encoded_path}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        except sqlite3.Error as exc:
            raise HistoryReadError(
                f"Impossible d'ouvrir la base en lecture seule: {self.database_path}"
            ) from exc

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _has_workflow_table(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'workflow_reports'
            """
        ).fetchone()
        return row is not None

    @staticmethod
    def _decode_report(raw: str, workflow_id: str) -> dict[str, Any]:
        try:
            report = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise HistoryReadError(
                f"Le rapport du workflow {workflow_id!r} est invalide"
            ) from exc
        if not isinstance(report, dict):
            raise HistoryReadError(
                f"Le rapport du workflow {workflow_id!r} n'est pas un objet JSON"
            )
        return report

    @staticmethod
    def _snapshot(
        row: sqlite3.Row,
        report: dict[str, Any],
    ) -> WorkflowSnapshot:
        iterations = report.get("iterations", [])
        if not isinstance(iterations, list):
            iterations = []

        final_verdict: str | None = None
        if iterations:
            final_result = iterations[-1].get("result", {})
            if isinstance(final_result, dict):
                verdict = final_result.get("proof_verdict")
                final_verdict = None if verdict is None else str(verdict)

        return WorkflowSnapshot(
            workflow_id=str(row["workflow_id"]),
            status=str(row["status"]),
            stop_reason=str(row["stop_reason"]),
            updated_at=str(row["updated_at"]),
            iteration_count=len(iterations),
            final_verdict=final_verdict,
        )

    def list_runs(self, limit: int = 50) -> tuple[WorkflowSnapshot, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("La limite doit être comprise entre 1 et 500")
        if not self.available:
            return ()

        try:
            with self._connection() as connection:
                if not self._has_workflow_table(connection):
                    return ()
                rows = connection.execute(
                    """
                    SELECT
                        workflow_id,
                        status,
                        stop_reason,
                        report_json,
                        updated_at
                    FROM workflow_reports
                    ORDER BY updated_at DESC, workflow_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryReadError(
                f"Impossible de lire l'historique: {self.database_path}"
            ) from exc

        snapshots: list[WorkflowSnapshot] = []
        for row in rows:
            workflow_id = str(row["workflow_id"])
            report = self._decode_report(row["report_json"], workflow_id)
            snapshots.append(self._snapshot(row, report))
        return tuple(snapshots)

    def get_run(self, workflow_id: str) -> WorkflowDetails | None:
        identifier = workflow_id.strip()
        if not identifier:
            raise ValueError("L'identifiant du workflow est obligatoire")
        if not self.available:
            return None

        try:
            with self._connection() as connection:
                if not self._has_workflow_table(connection):
                    return None
                row = connection.execute(
                    """
                    SELECT
                        workflow_id,
                        status,
                        stop_reason,
                        report_json,
                        updated_at
                    FROM workflow_reports
                    WHERE workflow_id = ?
                    """,
                    (identifier,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryReadError(
                f"Impossible de lire le workflow {identifier!r}"
            ) from exc

        if row is None:
            return None

        report = self._decode_report(row["report_json"], identifier)
        summary = self._snapshot(row, report)

        evidence: list[str] = []
        decisions: list[str] = []
        iterations = report.get("iterations", [])
        if isinstance(iterations, list):
            for position, iteration in enumerate(iterations, start=1):
                if not isinstance(iteration, dict):
                    continue
                result = iteration.get("result", {})
                if isinstance(result, dict):
                    values = result.get("evidence", [])
                    if isinstance(values, list):
                        evidence.extend(str(item) for item in values)

                decision = iteration.get("decision", {})
                if isinstance(decision, dict):
                    reason = str(decision.get("reason", "unknown"))
                    message = str(decision.get("message", "")).strip()
                    suffix = f" — {message}" if message else ""
                    decisions.append(f"Itération {position}: {reason}{suffix}")

        proposals: list[str] = []
        raw_proposals = report.get("learning_proposals", [])
        if isinstance(raw_proposals, list):
            for proposal in raw_proposals:
                if isinstance(proposal, dict):
                    identifier_value = str(proposal.get("proposal_id", "proposal"))
                    pattern = str(proposal.get("pattern", "")).strip()
                    proposals.append(
                        f"{identifier_value}: {pattern}"
                        if pattern
                        else identifier_value
                    )
                else:
                    proposals.append(str(proposal))

        memory = report.get("memory_snapshot", {})
        memory_keys = (
            tuple(sorted(str(key) for key in memory))
            if isinstance(memory, dict)
            else ()
        )

        return WorkflowDetails(
            summary=summary,
            evidence=tuple(evidence),
            decisions=tuple(decisions),
            learning_proposals=tuple(proposals),
            memory_keys=memory_keys,
        )
