import errno
import os
import threading
from pathlib import Path

import pytest

from sortl import service
from sortl.executor import ExecutionStatus
from sortl.models import OperationStatus
from sortl.service import (
    FolderCheckError,
    count_needing_attention,
    group_by_category,
    inspect_history,
    load_history,
    probe_hardlink_support,
    run_plan,
    scan_folder,
)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "messy"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"image-a")
    (root / "b.png").write_bytes(b"image-b")
    (root / "notes.txt").write_bytes(b"hello")
    (root / "data.csv").write_bytes(b"1,2,3")
    (root / "weird.xyz").write_bytes(b"other")
    (root / "subdir").mkdir()
    return root


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "journal.sqlite3"


def test_group_by_category_is_sorted(folder: Path) -> None:
    groups = group_by_category(scan_folder(folder).operations)

    assert list(groups) == ["Documents", "Images", "Other", "Spreadsheets"]
    assert [op.source.name for op in groups["Images"]] == ["a.jpg", "b.png"]


def test_probe_succeeds_and_leaves_no_files(folder: Path) -> None:
    before = sorted(p.name for p in folder.iterdir())

    probe_hardlink_support(folder)

    assert sorted(p.name for p in folder.iterdir()) == before


def test_probe_reports_missing_hardlink_support_and_cleans_up(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(service.os, "link", no_link)
    before = sorted(p.name for p in folder.iterdir())

    with pytest.raises(FolderCheckError, match="hard links"):
        probe_hardlink_support(folder)

    assert sorted(p.name for p in folder.iterdir()) == before


def test_probe_reports_unwritable_folder(tmp_path: Path) -> None:
    with pytest.raises(FolderCheckError, match="can't write"):
        probe_hardlink_support(tmp_path / "does-not-exist")


def test_probe_reports_inconsistent_identity(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_lstat = os.lstat
    calls = {"n": 0}

    def fake_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        info = real_lstat(path)
        calls["n"] += 1
        if calls["n"] == 2:
            values = list(info)
            values[1] += 1  # st_ino
            return os.stat_result(values)
        return info

    monkeypatch.setattr(service.os, "lstat", fake_lstat)

    with pytest.raises(FolderCheckError, match="identities"):
        probe_hardlink_support(folder)

    assert not [p for p in folder.iterdir() if p.name.startswith(".sortl-probe")]


def test_run_plan_moves_files_and_journals_them(
    folder: Path, journal_path: Path
) -> None:
    seen: list[tuple[int, int, str]] = []

    summary = run_plan(
        scan_folder(folder),
        journal_path,
        progress=lambda done, total, path: seen.append(
            (done, total, path.name)
        ),
    )

    assert summary.total == 5
    assert summary.completed == 5
    assert summary.blocked == summary.recovery_required == 0
    assert summary.categories_used == 4
    assert not summary.cancelled and not summary.halted
    assert (folder / "Images" / "a.jpg").read_bytes() == b"image-a"
    assert not (folder / "a.jpg").exists()
    assert [s[0] for s in seen] == [1, 2, 3, 4, 5]
    assert all(s[1] == 5 for s in seen)

    entries = load_history(journal_path)
    assert len(entries) == 5
    assert {e.status for e in entries} == {OperationStatus.COMPLETED}
    assert count_needing_attention(entries) == 0


def test_run_plan_skips_conflicts_without_overwriting(
    folder: Path, journal_path: Path
) -> None:
    (folder / "Images").mkdir()
    (folder / "Images" / "a.jpg").write_bytes(b"precious original")

    summary = run_plan(scan_folder(folder), journal_path)

    assert [op.source.name for op in summary.skipped_conflicts] == ["a.jpg"]
    assert summary.completed == 4
    assert (folder / "Images" / "a.jpg").read_bytes() == b"precious original"
    assert (folder / "a.jpg").read_bytes() == b"image-a"


def test_run_plan_cancel_stops_between_files(
    folder: Path, journal_path: Path
) -> None:
    cancel = threading.Event()

    def progress(done: int, total: int, path: Path) -> None:
        if done == 2:
            cancel.set()

    summary = run_plan(
        scan_folder(folder), journal_path, progress=progress, cancel=cancel
    )

    assert summary.cancelled
    assert summary.completed == 2
    assert summary.not_attempted == 3
    assert len(list(p for p in folder.iterdir() if p.is_file())) == 3


def test_run_plan_blocks_changed_file_but_continues(
    folder: Path, journal_path: Path
) -> None:
    plan = scan_folder(folder)
    (folder / "notes.txt").write_bytes(b"edited after planning")

    summary = run_plan(plan, journal_path)

    assert summary.blocked == 1
    assert summary.completed == 4
    assert (folder / "notes.txt").read_bytes() == b"edited after planning"
    assert not (folder / "Documents").exists()


def test_run_plan_halts_on_first_recovery_required(
    folder: Path, journal_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sortl.executor as executor

    real_link = executor.os.link
    calls = {"n": 0}

    def flaky_link(src, dst, *, follow_symlinks=True):  # type: ignore[no-untyped-def]
        # The probe runs first and must succeed; fail the executor's first link.
        if Path(src).name.startswith(".sortl-probe"):
            return real_link(src, dst, follow_symlinks=follow_symlinks)
        calls["n"] += 1
        raise OSError(errno.EIO, "disk on fire")

    monkeypatch.setattr(executor.os, "link", flaky_link)

    summary = run_plan(scan_folder(folder), journal_path)

    assert summary.halted
    assert summary.recovery_required == 1
    assert summary.completed == 0
    assert summary.not_attempted == 4
    assert calls["n"] == 1
    # Nothing was lost: all original files are still in place.
    assert len([p for p in folder.iterdir() if p.is_file()]) == 5

    entries = load_history(journal_path)
    assert count_needing_attention(entries) == 1


def test_run_plan_refuses_when_hardlinks_unsupported(
    folder: Path, journal_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(service.os, "link", no_link)
    plan = scan_folder(folder)

    with pytest.raises(FolderCheckError):
        run_plan(plan, journal_path)

    assert not journal_path.exists()
    assert not (folder / "Images").exists()


def test_history_is_empty_without_a_journal(journal_path: Path) -> None:
    assert load_history(journal_path) == ()
    assert inspect_history(journal_path) == {}


def test_inspect_history_reports_findings(
    folder: Path, journal_path: Path
) -> None:
    run_plan(scan_folder(folder), journal_path)

    findings = inspect_history(journal_path)

    assert len(findings) == 5
    assert {f.assessment.value for f in findings.values()} == {
        "completed_consistent"
    }
    assert ExecutionStatus.COMPLETED.value == "completed"
