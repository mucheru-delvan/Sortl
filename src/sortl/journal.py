
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from sortl.models import FileOperation, OperationStatus


class JournalError(RuntimeError):
    """Base exception for operation journal errors."""


class DuplicateOperationError(JournalError):
    """Raised when an operation ID already exists."""


class OperationNotFoundError(JournalError):
    """Raised when an operation cannot be found."""


class InvalidJournalTransition(JournalError):
    """Raised when an operation status transition is invalid."""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """Immutable representation of a persisted file operation."""

    operation_id: str
    source: Path
    destination: Path
    sha256: str
    status: OperationStatus
    created_at: str
    updated_at: str
    error: str | None


def _utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _entry_from_row(row: sqlite3.Row) -> JournalEntry:
    """Convert a SQLite row into a journal entry."""
    return JournalEntry(
        operation_id=row["operation_id"],
        source=Path(row["source"]),
        destination=Path(row["destination"]),
        sha256=row["sha256"],
        status=OperationStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error=row["error"],
    )


class OperationJournal:
    """Persist file-operation history using SQLite.

    The journal records intended operations and their states. It does not
    perform filesystem moves or establish that a move actually occurred.
    """

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)
        self._connection = sqlite3.connect(
            str(self.database),
            timeout=5.0,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

        try:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    sha256 TEXT NOT NULL CHECK (
                        length(sha256) = 64
                        AND sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'planned',
                            'in_progress',
                            'completed',
                            'rolled_back',
                            'recovery_required'
                        )
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    CHECK (
                        (
                            status = 'recovery_required'
                            AND error IS NOT NULL
                            AND length(trim(error)) > 0
                        )
                        OR (
                            status != 'recovery_required'
                            AND error IS NULL
                        )
                    )
                )
                """
            )
        except Exception:
            self._connection.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run database changes in an explicit transaction."""
        self._connection.execute("BEGIN IMMEDIATE")

        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def create(self, operation: FileOperation) -> JournalEntry:
        """Persist a new operation in the planned state."""
        if operation.status != OperationStatus.PLANNED:
            raise ValueError("New operations must have planned status")

        if not operation.source.is_absolute():
            raise ValueError("Source path must be absolute")

        if not operation.destination.is_absolute():
            raise ValueError("Destination path must be absolute")

        if operation.source == operation.destination:
            raise ValueError("Source and destination must differ")

        if (
            len(operation.sha256) != 64
            or any(char not in "0123456789abcdef" for char in operation.sha256)
        ):
            raise ValueError("sha256 must be a lowercase hexadecimal digest")

        now = _utc_now()

        try:
            with self._transaction():
                self._connection.execute(
                    """
                    INSERT INTO operations (
                        operation_id,
                        source,
                        destination,
                        sha256,
                        status,
                        created_at,
                        updated_at,
                        error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        operation.operation_id,
                        str(operation.source),
                        str(operation.destination),
                        operation.sha256,
                        OperationStatus.PLANNED.value,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "operations.operation_id" in str(exc):
                raise DuplicateOperationError(
                    f"Operation already exists: {operation.operation_id}"
                ) from exc
            raise

        entry = self.get(operation.operation_id)

        if entry is None:
            raise JournalError(
                f"Operation disappeared after creation: {operation.operation_id}"
            )

        return entry

    def get(self, operation_id: str) -> JournalEntry | None:
        """Return an operation by ID, or None if it does not exist."""
        row = self._connection.execute(
            """
            SELECT *
            FROM operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()

        return _entry_from_row(row) if row is not None else None

    def list_entries(self) -> tuple[JournalEntry, ...]:
        """Return all journal entries in deterministic order."""
        rows = self._connection.execute(
            """
            SELECT *
            FROM operations
            ORDER BY created_at, operation_id
            """
        ).fetchall()

        return tuple(_entry_from_row(row) for row in rows)

    def transition(
        self,
        operation_id: str,
        new_status: OperationStatus,
        *,
        error: str | None = None,
    ) -> JournalEntry:
        """Validate and persist an operation status transition.

        A recovery-required state must include a non-empty explanation.
        Other states must not carry an error message.
        """
        if not isinstance(new_status, OperationStatus):
            raise TypeError("new_status must be an OperationStatus")

        if new_status == OperationStatus.RECOVERY_REQUIRED:
            if error is None or not error.strip():
                raise ValueError(
                    "A non-empty error message is required for recovery_required"
                )
            error = error.strip()
        elif error is not None:
            raise ValueError(
                "An error message is only allowed for recovery_required"
            )

        with self._transaction():
            row = self._connection.execute(
                """
                SELECT *
                FROM operations
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()

            if row is None:
                raise OperationNotFoundError(
                    f"Operation not found: {operation_id}"
                )

            operation = FileOperation(
                source=Path(row["source"]),
                destination=Path(row["destination"]),
                sha256=row["sha256"],
                operation_id=row["operation_id"],
                status=OperationStatus(row["status"]),
            )

            try:
                operation.transition_to(new_status)
            except ValueError as exc:
                raise InvalidJournalTransition(str(exc)) from exc

            self._connection.execute(
                """
                UPDATE operations
                SET status = ?, updated_at = ?, error = ?
                WHERE operation_id = ?
                """,
                (
                    new_status.value,
                    _utc_now(),
                    error,
                    operation_id,
                ),
            )

            updated_row = self._connection.execute(
                """
                SELECT *
                FROM operations
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()

            if updated_row is None:
                raise JournalError(
                    f"Operation disappeared after transition: {operation_id}"
                )

            return _entry_from_row(updated_row)

    def close(self) -> None:
        """Close the database connection."""
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
