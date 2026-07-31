"""Tenant-isolated transactional persistence for ELMAN-OS.

The public boundary is deliberately backend-neutral.  SQLite is provided for
local and single-node deployments; another backend (notably PostgreSQL) can
implement ``PersistenceBackend`` without changing tenant-facing code.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MAX_VALUE_BYTES = 1024 * 1024


class PersistenceError(RuntimeError):
    """Base error that does not leak database implementation details."""


class PersistenceConflictError(PersistenceError):
    """Optimistic concurrency precondition failed."""


class PersistenceIntegrityError(PersistenceError):
    """Stored data is malformed or violates the persistence contract."""


class TransactionClosedError(PersistenceError):
    """Operation attempted outside an active transaction."""


@dataclass(frozen=True, slots=True)
class StoredRecord:
    tenant_id: str
    namespace: str
    key: str
    value: Mapping[str, Any]
    version: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.version < 1:
            raise PersistenceIntegrityError("La version stockée doit être positive")
        object.__setattr__(self, "value", MappingProxyType(dict(self.value)))


@runtime_checkable
class PersistenceTransaction(Protocol):
    async def get(self, key: str) -> StoredRecord | None: ...

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        expected_version: int | None,
    ) -> StoredRecord: ...

    async def delete(
        self,
        key: str,
        *,
        expected_version: int | None,
    ) -> bool: ...

    async def list(
        self,
        *,
        prefix: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[StoredRecord, ...]: ...


@runtime_checkable
class PersistenceBackend(Protocol):
    """Backend contract suitable for SQLite or a PostgreSQL adapter."""

    def transaction(
        self,
        tenant_id: str,
        namespace: str = "default",
    ) -> "TransactionContext": ...

    async def close(self) -> None: ...


def _safe_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} doit être un identifiant sûr")
    return value


def _safe_key(key: str) -> str:
    return _safe_id("key", key)


def _encode_value(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("value doit être un objet de type Mapping")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value doit être un objet JSON strict") from exc
    if len(encoded.encode("utf-8")) > _MAX_VALUE_BYTES:
        raise ValueError("value dépasse la taille maximale autorisée")
    return encoded


def _decode_row(row: sqlite3.Row) -> StoredRecord:
    try:
        value = json.loads(row["value_json"])
        if not isinstance(value, dict):
            raise TypeError
        return StoredRecord(
            tenant_id=str(row["tenant_id"]),
            namespace=str(row["namespace"]),
            key=str(row["record_key"]),
            value=value,
            version=int(row["version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PersistenceIntegrityError(
            "Un enregistrement persistant est invalide"
        ) from exc


class TransactionContext(Protocol):
    async def __aenter__(self) -> PersistenceTransaction: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool: ...


class SQLitePersistence:
    """Durable SQLite backend with tenant scoping and optimistic locking."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        self.path = Path(path)
        if self.path.exists() and self.path.is_symlink():
            raise PersistenceError("La base SQLite ne peut pas être un lien symbolique")
        if busy_timeout_seconds <= 0 or busy_timeout_seconds > 60:
            raise ValueError("busy_timeout_seconds doit être compris entre 0 et 60")
        self.busy_timeout_seconds = busy_timeout_seconds
        self._lock = asyncio.Lock()
        self._closed = False
        self._initialized = False

    def transaction(
        self,
        tenant_id: str,
        namespace: str = "default",
    ) -> "_SQLiteTransaction":
        if self._closed:
            raise PersistenceError("Le backend de persistance est fermé")
        return _SQLiteTransaction(
            backend=self,
            tenant_id=_safe_id("tenant_id", tenant_id),
            namespace=_safe_id("namespace", namespace),
        )

    async def close(self) -> None:
        self._closed = True

    async def _initialize(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.is_symlink():
            raise PersistenceError("La base SQLite ne peut pas être un lien symbolique")
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS elman_records (
                    tenant_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, namespace, record_key)
                );
                CREATE INDEX IF NOT EXISTS idx_elman_records_scope
                    ON elman_records (tenant_id, namespace, record_key);
                """
            )
        except sqlite3.Error as exc:
            raise PersistenceError(
                "L'initialisation de la persistance a échoué"
            ) from exc
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(
            f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}"
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


class _SQLiteTransaction:
    def __init__(
        self,
        *,
        backend: SQLitePersistence,
        tenant_id: str,
        namespace: str,
    ) -> None:
        self._backend = backend
        self._tenant_id = tenant_id
        self._namespace = namespace
        self._connection: sqlite3.Connection | None = None
        self._entered = False
        self._lock_acquired = False

    async def __aenter__(self) -> "_SQLiteTransaction":
        if self._entered:
            raise PersistenceError("Une transaction ne peut être réutilisée")
        await self._backend._initialize()
        await self._backend._lock.acquire()
        self._lock_acquired = True
        try:
            self._connection = await asyncio.to_thread(self._backend._connect)
            await asyncio.to_thread(self._connection.execute, "BEGIN IMMEDIATE")
            self._entered = True
            return self
        except (sqlite3.Error, OSError) as exc:
            await self._release()
            raise PersistenceError("La transaction n'a pas pu démarrer") from exc

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        connection = self._connection
        if connection is None:
            await self._release()
            return False
        try:
            if exc_type is None:
                await asyncio.to_thread(connection.commit)
            else:
                await asyncio.to_thread(connection.rollback)
        except sqlite3.Error as db_exc:
            raise PersistenceError(
                "La finalisation de la transaction a échoué"
            ) from db_exc
        finally:
            await asyncio.to_thread(connection.close)
            self._connection = None
            self._entered = False
            await self._release()
        return False

    async def _release(self) -> None:
        if self._lock_acquired:
            self._backend._lock.release()
            self._lock_acquired = False

    def _active(self) -> sqlite3.Connection:
        if not self._entered or self._connection is None:
            raise TransactionClosedError("La transaction n'est pas active")
        return self._connection

    async def get(self, key: str) -> StoredRecord | None:
        key = _safe_key(key)
        connection = self._active()
        try:
            cursor = await asyncio.to_thread(
                connection.execute,
                """
                SELECT tenant_id, namespace, record_key, value_json,
                       version, created_at, updated_at
                FROM elman_records
                WHERE tenant_id = ? AND namespace = ? AND record_key = ?
                """,
                (self._tenant_id, self._namespace, key),
            )
            row = await asyncio.to_thread(cursor.fetchone)
            return None if row is None else _decode_row(row)
        except sqlite3.Error as exc:
            raise PersistenceError("La lecture persistante a échoué") from exc

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        expected_version: int | None,
    ) -> StoredRecord:
        key = _safe_key(key)
        encoded = _encode_value(value)
        if expected_version is not None and expected_version < 0:
            raise ValueError("expected_version ne peut pas être négative")
        connection = self._active()
        now = datetime.now(timezone.utc).isoformat()
        try:
            if expected_version == 0:
                try:
                    await asyncio.to_thread(
                        connection.execute,
                        """
                        INSERT INTO elman_records (
                            tenant_id, namespace, record_key, value_json,
                            version, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            self._tenant_id,
                            self._namespace,
                            key,
                            encoded,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise PersistenceConflictError(
                        "L'enregistrement existe déjà"
                    ) from exc
            elif expected_version is None:
                await asyncio.to_thread(
                    connection.execute,
                    """
                    INSERT INTO elman_records (
                        tenant_id, namespace, record_key, value_json,
                        version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT (tenant_id, namespace, record_key)
                    DO UPDATE SET
                        value_json = excluded.value_json,
                        version = elman_records.version + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self._tenant_id,
                        self._namespace,
                        key,
                        encoded,
                        now,
                        now,
                    ),
                )
            else:
                cursor = await asyncio.to_thread(
                    connection.execute,
                    """
                    UPDATE elman_records
                    SET value_json = ?, version = version + 1, updated_at = ?
                    WHERE tenant_id = ? AND namespace = ? AND record_key = ?
                      AND version = ?
                    """,
                    (
                        encoded,
                        now,
                        self._tenant_id,
                        self._namespace,
                        key,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PersistenceConflictError(
                        "La version attendue ne correspond pas"
                    )
            record = await self.get(key)
            if record is None:
                raise PersistenceIntegrityError(
                    "L'écriture n'a produit aucun enregistrement"
                )
            return record
        except PersistenceError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("L'écriture persistante a échoué") from exc

    async def delete(
        self,
        key: str,
        *,
        expected_version: int | None,
    ) -> bool:
        key = _safe_key(key)
        if expected_version is not None and expected_version < 1:
            raise ValueError("expected_version doit être positive")
        connection = self._active()
        query = """
            DELETE FROM elman_records
            WHERE tenant_id = ? AND namespace = ? AND record_key = ?
        """
        parameters: tuple[object, ...] = (
            self._tenant_id,
            self._namespace,
            key,
        )
        if expected_version is not None:
            query += " AND version = ?"
            parameters += (expected_version,)
        try:
            cursor = await asyncio.to_thread(
                connection.execute,
                query,
                parameters,
            )
            if cursor.rowcount == 1:
                return True
            if expected_version is not None:
                exists = await self.get(key)
                if exists is not None:
                    raise PersistenceConflictError(
                        "La version attendue ne correspond pas"
                    )
            return False
        except PersistenceError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("La suppression persistante a échoué") from exc

    async def list(
        self,
        *,
        prefix: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[StoredRecord, ...]:
        if prefix:
            _safe_key(prefix)
        if limit < 1 or limit > 1000:
            raise ValueError("limit doit être compris entre 1 et 1000")
        if offset < 0:
            raise ValueError("offset ne peut pas être négatif")
        connection = self._active()
        escaped = (
            prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        try:
            cursor = await asyncio.to_thread(
                connection.execute,
                """
                SELECT tenant_id, namespace, record_key, value_json,
                       version, created_at, updated_at
                FROM elman_records
                WHERE tenant_id = ? AND namespace = ?
                  AND record_key LIKE ? ESCAPE '\\'
                ORDER BY record_key
                LIMIT ? OFFSET ?
                """,
                (
                    self._tenant_id,
                    self._namespace,
                    f"{escaped}%",
                    limit,
                    offset,
                ),
            )
            rows: Sequence[sqlite3.Row] = await asyncio.to_thread(cursor.fetchall)
            return tuple(_decode_row(row) for row in rows)
        except sqlite3.Error as exc:
            raise PersistenceError("L'énumération persistante a échoué") from exc

