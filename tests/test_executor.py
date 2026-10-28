import os
from pathlib import Path

import pytest

import sortl.executor as executor_module
from sortl.executor import ExecutionStatus, execute_plan
from sortl.journal import OperationJournal
from sortl.models import OperationStatus
from sortl.planner import Plan, PlannedFile, create_plan


def _create_downloads(
    tmp_path: Path,
    content: bytes = b"Sortl test document",
) -> tuple[Path, Path]:
    root = tmp_path / "Downloads"
    root.mkdir()
    source = root / "report.pdf"
    source.write_bytes(content)
    return root, source


def test_dry_run_is_default_and_does_not_modify_files_or_journal(
    tmp_path: Path,
) -> None:
    root, source = _create_downloads(tmp_path)
    plan = create_plan(root)

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal)

        assert report.dry_run
        assert report.results[0].status == ExecutionStatus.DRY_RUN
        assert report.completed_count == 0
        assert source.is_file()
        assert not (root / "Documents").exists()
        assert journal.list_entries() == ()


def test_execute_moves_file_and_completes_journal(
    tmp_path: Path,
) -> None:
    root, source = _create_downloads(tmp_path)
    plan = create_plan(root)
    destination = root / "Documents" / source.name

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.COMPLETED
        assert report.completed_count == 1
        assert not source.exists()
        assert destination.read_bytes() == b"Sortl test document"

        entries = journal.list_entries()
        assert len(entries) == 1
        assert entries[0].status == OperationStatus.COMPLETED
        assert entries[0].source == source
        assert entries[0].destination == destination


def test_changed_source_is_blocked_without_journaling(
    tmp_path: Path,
) -> None:
    root, source = _create_downloads(tmp_path)
    plan = create_plan(root)
    source.write_bytes(b"Changed after planning")

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.BLOCKED
        assert "changed since planning" in report.results[0].message
        assert source.read_bytes() == b"Changed after planning"
        assert not (root / "Documents").exists()
        assert journal.list_entries() == ()


def test_existing_destination_is_never_overwritten(
    tmp_path: Path,
) -> None:
    root, source = _create_downloads(tmp_path)
    destination = root / "Documents" / source.name
    destination.parent.mkdir()
    destination.write_bytes(b"Keep this file")
    plan = create_plan(root)

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.BLOCKED
        assert source.read_bytes() == b"Sortl test document"
        assert destination.read_bytes() == b"Keep this file"
        assert journal.list_entries() == ()


def test_destination_created_after_planning_is_blocked(
    tmp_path: Path,
) -> None:
    root, source = _create_downloads(tmp_path)
    plan = create_plan(root)
    destination = root / "Documents" / source.name
    destination.parent.mkdir()
    destination.write_bytes(b"Created after planning")

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.BLOCKED
        assert destination.read_bytes() == b"Created after planning"
        assert source.read_bytes() == b"Sortl test document"
        assert journal.list_entries() == ()


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="This test requires symlink-resistant file opening",
)
def test_source_replaced_by_symlink_is_blocked(
    tmp_path: Path,
) -> None:
    root, source = _create_downloads(tmp_path)
    plan = create_plan(root)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"Do not move this target")

    source.unlink()
    source.symlink_to(outside)

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.BLOCKED
        assert source.is_symlink()
        assert outside.read_bytes() == b"Do not move this target"
        assert journal.list_entries() == ()


@pytest.mark.skipif(
    os.name == "nt",
    reason="This test requires POSIX directory symlinks",
)
def test_destination_directory_symlink_is_blocked(
    tmp_path: Path,
) -> None:
    root, source = _create_downloads(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Documents").symlink_to(outside, target_is_directory=True)
    plan = create_plan(root)

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.BLOCKED
        assert source.is_file()
        assert not (outside / source.name).exists()
        assert journal.list_entries() == ()


def test_move_failure_sets_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source = _create_downloads(tmp_path)
    plan = create_plan(root)

    def fail_link(
        source_path: Path,
        destination_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        raise OSError("simulated hard-link failure")

    monkeypatch.setattr(executor_module.os, "link", fail_link)

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.RECOVERY_REQUIRED
        assert source.read_bytes() == b"Sortl test document"
        assert not (root / "Documents" / source.name).exists()

        entry = journal.list_entries()[0]
        assert entry.status == OperationStatus.RECOVERY_REQUIRED
        assert "simulated hard-link failure" in (entry.error or "")


def test_destination_race_does_not_overwrite_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source = _create_downloads(tmp_path)
    plan = create_plan(root)
    destination = root / "Documents" / source.name
    original_link = os.link

    def create_conflict_then_link(
        source_path: Path,
        destination_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        Path(destination_path).write_bytes(b"Created during execution")
        original_link(
            source_path,
            destination_path,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(
        executor_module.os,
        "link",
        create_conflict_then_link,
    )

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.RECOVERY_REQUIRED
        assert source.read_bytes() == b"Sortl test document"
        assert destination.read_bytes() == b"Created during execution"
        assert journal.list_entries()[0].status == (
            OperationStatus.RECOVERY_REQUIRED
        )


def test_source_outside_root_is_blocked(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Downloads"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Outside file")

    plan = Plan(
        root=root.resolve(),
        operations=(
            PlannedFile(
                source=outside,
                destination=root / "Documents" / outside.name,
                sha256=(
                    "b" * 64
                ),
            ),
        ),
        errors=(),
    )

    with OperationJournal(tmp_path / "journal.db") as journal:
        report = execute_plan(plan, journal, dry_run=False)

        assert report.results[0].status == ExecutionStatus.BLOCKED
        assert outside.read_bytes() == b"Outside file"
        assert journal.list_entries() == ()
