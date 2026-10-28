import hashlib
import os
from pathlib import Path

from sortl.journal import OperationJournal
from sortl.models import FileOperation, OperationStatus
from sortl.recovery import RecoveryAssessment, inspect_journal

CONTENT = b"Sortl recovery test"
CHANGED_CONTENT = b"Different content"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _create_entry(
    journal: OperationJournal,
    source: Path,
    destination: Path,
):
    return journal.create(
        FileOperation.create(
            source=source,
            destination=destination,
            sha256=_sha256(source.read_bytes()),
        )
    )


def _move_status_to_in_progress(
    journal: OperationJournal,
    operation_id: str,
) -> None:
    journal.transition(operation_id, OperationStatus.IN_PROGRESS)


def test_planned_source_only_is_not_started(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)

        report = inspect_journal(journal)

        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.operation_id == entry.operation_id
        assert finding.assessment == RecoveryAssessment.NOT_STARTED
        assert finding.source_exists
        assert not finding.destination_exists
        assert journal.get(entry.operation_id).status == OperationStatus.PLANNED


def test_same_file_at_both_paths_suggests_interrupted_link(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name
    destination.parent.mkdir()

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        _move_status_to_in_progress(journal, entry.operation_id)
        os.link(source, destination)

        report = inspect_journal(journal)

        assert (
            report.findings[0].assessment
            == RecoveryAssessment.LINKED_BOTH_NAMES
        )
        assert source.exists()
        assert destination.exists()
        assert journal.get(entry.operation_id).status == (
            OperationStatus.IN_PROGRESS
        )


def test_destination_only_matching_hash_may_have_completed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name
    destination.parent.mkdir()

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        _move_status_to_in_progress(journal, entry.operation_id)
        os.link(source, destination)
        source.unlink()

        finding = inspect_journal(journal).findings[0]

        assert finding.assessment == (
            RecoveryAssessment.MOVE_MAY_HAVE_COMPLETED
        )
        assert not finding.source_exists
        assert finding.destination_exists
        assert destination.read_bytes() == CONTENT
        assert journal.get(entry.operation_id).status == (
            OperationStatus.IN_PROGRESS
        )


def test_completed_entry_matches_filesystem(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name
    destination.parent.mkdir()

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        _move_status_to_in_progress(journal, entry.operation_id)
        os.link(source, destination)
        source.unlink()
        journal.transition(
            entry.operation_id,
            OperationStatus.COMPLETED,
        )

        finding = inspect_journal(journal).findings[0]

        assert finding.assessment == RecoveryAssessment.COMPLETED_CONSISTENT
        assert not finding.source_exists
        assert finding.destination_sha256 == _sha256(CONTENT)


def test_rolled_back_entry_matches_filesystem(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name
    destination.parent.mkdir()

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        _move_status_to_in_progress(journal, entry.operation_id)
        os.link(source, destination)
        source.unlink()
        journal.transition(
            entry.operation_id,
            OperationStatus.COMPLETED,
        )

        os.link(destination, source)
        destination.unlink()
        journal.transition(
            entry.operation_id,
            OperationStatus.ROLLED_BACK,
        )

        finding = inspect_journal(journal).findings[0]

        assert finding.assessment == (
            RecoveryAssessment.ROLLED_BACK_CONSISTENT
        )
        assert source.read_bytes() == CONTENT
        assert not destination.exists()


def test_changed_source_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        source.write_bytes(CHANGED_CONTENT)

        finding = inspect_journal(journal).findings[0]

        assert finding.assessment == RecoveryAssessment.SOURCE_CHANGED
        assert finding.source_sha256 == _sha256(CHANGED_CONTENT)
        assert journal.get(entry.operation_id).status == OperationStatus.PLANNED


def test_different_existing_files_are_a_conflict(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name
    destination.parent.mkdir()
    destination.write_bytes(CHANGED_CONTENT)

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        _move_status_to_in_progress(journal, entry.operation_id)

        finding = inspect_journal(journal).findings[0]

        assert finding.assessment == RecoveryAssessment.DESTINATION_CONFLICT
        assert source.read_bytes() == CONTENT
        assert destination.read_bytes() == CHANGED_CONTENT


def test_mismatched_destination_hash_is_reported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name
    destination.parent.mkdir()

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        _move_status_to_in_progress(journal, entry.operation_id)
        source.unlink()
        destination.write_bytes(CHANGED_CONTENT)

        finding = inspect_journal(journal).findings[0]

        assert finding.assessment == (
            RecoveryAssessment.DESTINATION_HASH_MISMATCH
        )
        assert finding.destination_sha256 == _sha256(CHANGED_CONTENT)


def test_missing_source_and_destination_are_reported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    destination = tmp_path / "Documents" / source.name

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        _move_status_to_in_progress(journal, entry.operation_id)
        source.unlink()

        finding = inspect_journal(journal).findings[0]

        assert finding.assessment == RecoveryAssessment.FILES_MISSING
        assert not finding.source_exists
        assert not finding.destination_exists


def test_symlink_source_is_reported_as_unsafe(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(CONTENT)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Do not touch this file")
    destination = tmp_path / "Documents" / source.name

    with OperationJournal(tmp_path / "journal.db") as journal:
        entry = _create_entry(journal, source, destination)
        source.unlink()
        source.symlink_to(outside)

        finding = inspect_journal(journal).findings[0]

        assert finding.assessment == RecoveryAssessment.UNSAFE_PATH
        assert outside.read_bytes() == b"Do not touch this file"
        assert journal.get(entry.operation_id).status == OperationStatus.PLANNED