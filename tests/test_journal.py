import sqlite3
from pathlib import Path

import pytest

from sortl.journal import (
    DuplicateOperationError,
    InvalidJournalTransition,
    OperationJournal,
    OperationNotFoundError,
)
from sortl.models import FileOperation, OperationStatus

VALID_HASH = "a" * 64


def make_operation() -> FileOperation:
    return FileOperation.create(
        source=Path("/tmp/sortl-source.txt"),
        destination=Path("/tmp/Documents/sortl-source.txt"),
        sha256=VALID_HASH,
    )


def test_create_and_retrieve_operation(tmp_path: Path):
    database = tmp_path / "journal.db"

    with OperationJournal(database) as journal:
        created = journal.create(make_operation())
        retrieved = journal.get(created.operation_id)

        assert retrieved == created
        assert retrieved is not None
        assert retrieved.status == OperationStatus.PLANNED
        assert retrieved.source == Path("/tmp/sortl-source.txt")
        assert retrieved.destination == Path(
            "/tmp/Documents/sortl-source.txt"
        )
        assert retrieved.sha256 == VALID_HASH
        assert retrieved.error is None


def test_entry_survives_database_reopening(tmp_path: Path):
    database = tmp_path / "journal.db"
    operation = make_operation()

    with OperationJournal(database) as journal:
        journal.create(operation)

    with OperationJournal(database) as journal:
        retrieved = journal.get(operation.operation_id)

    assert retrieved is not None
    assert retrieved.operation_id == operation.operation_id
    assert retrieved.status == OperationStatus.PLANNED


def test_duplicate_operation_id_is_rejected(tmp_path: Path):
    with OperationJournal(tmp_path / "journal.db") as journal:
        operation = make_operation()
        journal.create(operation)

        with pytest.raises(DuplicateOperationError):
            journal.create(operation)

        assert len(journal.list_entries()) == 1


def test_list_entries_returns_all_records(tmp_path: Path):
    with OperationJournal(tmp_path / "journal.db") as journal:
        first = make_operation()
        second = make_operation()

        journal.create(first)
        journal.create(second)

        entries = journal.list_entries()

        assert len(entries) == 2
        assert {entry.operation_id for entry in entries} == {
            first.operation_id,
            second.operation_id,
        }


def test_valid_status_transition_is_persisted(tmp_path: Path):
    with OperationJournal(tmp_path / "journal.db") as journal:
        operation = journal.create(make_operation())

        updated = journal.transition(
            operation.operation_id,
            OperationStatus.IN_PROGRESS,
        )

        assert updated.status == OperationStatus.IN_PROGRESS
        assert updated.updated_at >= operation.updated_at


def test_invalid_status_transition_is_rejected(tmp_path: Path):
    with OperationJournal(tmp_path / "journal.db") as journal:
        operation = journal.create(make_operation())

        with pytest.raises(InvalidJournalTransition):
            journal.transition(
                operation.operation_id,
                OperationStatus.COMPLETED,
            )

        unchanged = journal.get(operation.operation_id)
        assert unchanged is not None
        assert unchanged.status == OperationStatus.PLANNED


def test_missing_operation_is_reported(tmp_path: Path):
    with (
        OperationJournal(tmp_path / "journal.db") as journal,
        pytest.raises(OperationNotFoundError),
    ):
        journal.transition(
            "missing-operation",
            OperationStatus.IN_PROGRESS,
        )

def test_recovery_requires_a_reason(tmp_path: Path):
    with OperationJournal(tmp_path / "journal.db") as journal:
        operation = journal.create(make_operation())
        journal.transition(
            operation.operation_id,
            OperationStatus.IN_PROGRESS,
        )

        with pytest.raises(ValueError, match="non-empty error"):
            journal.transition(
                operation.operation_id,
                OperationStatus.RECOVERY_REQUIRED,
            )

        with pytest.raises(ValueError, match="non-empty error"):
            journal.transition(
                operation.operation_id,
                OperationStatus.RECOVERY_REQUIRED,
                error="   ",
            )


def test_recovery_reason_is_persisted_and_cleared_on_completion(
    tmp_path: Path,
):
    database = tmp_path / "journal.db"

    with OperationJournal(database) as journal:
        operation = journal.create(make_operation())
        journal.transition(
            operation.operation_id,
            OperationStatus.IN_PROGRESS,
        )

        recovery = journal.transition(
            operation.operation_id,
            OperationStatus.RECOVERY_REQUIRED,
            error="Destination state is uncertain",
        )

        assert recovery.error == "Destination state is uncertain"

    with OperationJournal(database) as journal:
        recovered = journal.get(operation.operation_id)
        assert recovered is not None
        assert recovered.status == OperationStatus.RECOVERY_REQUIRED
        assert recovered.error == "Destination state is uncertain"

        completed = journal.transition(
            operation.operation_id,
            OperationStatus.COMPLETED,
        )

        assert completed.status == OperationStatus.COMPLETED
        assert completed.error is None


def test_non_absolute_paths_are_rejected(tmp_path: Path):
    operation = FileOperation.create(
        source=Path("source.txt"),
        destination=Path("Documents/source.txt"),
        sha256=VALID_HASH,
    )

    with OperationJournal(tmp_path / "journal.db") as journal:
        with pytest.raises(ValueError, match="absolute"):
            journal.create(operation)

        assert journal.list_entries() == ()


def test_database_parent_is_not_created_implicitly(tmp_path: Path):
    database = tmp_path / "missing" / "journal.db"

    with pytest.raises(sqlite3.OperationalError):
        OperationJournal(database)

    assert not database.parent.exists()
