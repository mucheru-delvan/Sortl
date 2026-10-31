"""GUI-agnostic orchestration on top of the Sortl core.

Nothing in this module imports GTK, so everything here is unit-testable
without a display. The UI calls these functions from worker threads.

Threading rule: ``OperationJournal`` holds a SQLite connection that may only
be used on the thread that created it, so every function here that touches
the journal opens (and closes) its own journal instead of accepting one.
"""

from __future__ import annotations

import errno
import os
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sortl.executor import ExecutionResult, ExecutionStatus, execute_plan
from sortl.journal import JournalEntry, OperationJournal
from sortl.models import OperationStatus
from sortl.planner import Plan, PlannedFile, PlanningError, create_plan
from sortl.recovery import RecoveryFinding, inspect_journal

PROBE_PREFIX = ".sortl-probe-"


# Journal states that mean "something did not finish cleanly".
ATTENTION_STATUSES = frozenset(
    {
        OperationStatus.PLANNED,
        OperationStatus.IN_PROGRESS,
        OperationStatus.RECOVERY_REQUIRED,
    }
)

ProgressCallback = Callable[[int, int, Path], None]


class FolderCheckError(RuntimeError):
    """The chosen folder cannot be organized safely. Message is user-facing."""


def scan_folder(root: Path) -> Plan:
    """Build a read-only plan. Hashes every file, so run it off the UI thread."""
    return create_plan(root)


def group_by_category(
    operations: Iterable[PlannedFile],
) -> dict[str, list[PlannedFile]]:
    """Group planned files by destination category folder, sorted by name."""
    groups: dict[str, list[PlannedFile]] = {}

    for operation in operations:
        groups.setdefault(operation.destination.parent.name, []).append(
            operation
        )

    return dict(sorted(groups.items(), key=lambda item: item[0].casefold()))


def probe_hardlink_support(root: Path) -> None:
    """Verify that ``root`` is writable and supports hard links.

    The core executor moves files by creating a hard link and then removing
    the source name, with no copy fallback. On a filesystem without hard
    links (some FUSE mounts, FAT/exFAT, certain network shares) every move
    would fail into ``recovery_required``. Failing here instead means nothing
    is journaled and nothing is touched.

    The probe creates two hidden, uniquely named files and always tries to
    remove them. It deliberately mirrors the executor's identity check
    (``st_dev``, ``st_ino``).
    """
    root = Path(root)
    token = uuid4().hex
    first = root / f"{PROBE_PREFIX}{token}"
    second = root / f"{PROBE_PREFIX}{token}.link"
    created: list[Path] = []

    try:
        try:
            fd = os.open(
                first,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as exc:
            raise FolderCheckError(
                "Sortl can't write to this folder "
                f"({exc.strerror or errno.errorcode.get(exc.errno, 'error')})."
            ) from exc

        created.append(first)
        os.close(fd)

        try:
            os.link(first, second, follow_symlinks=False)
        except (OSError, NotImplementedError) as exc:
            raise FolderCheckError(
                "This folder's filesystem doesn't support hard links, which "
                "Sortl needs to move files without any risk of losing them."
            ) from exc

        created.append(second)

        first_info = os.lstat(first)
        second_info = os.lstat(second)

        if (first_info.st_dev, first_info.st_ino) != (
            second_info.st_dev,
            second_info.st_ino,
        ):
            raise FolderCheckError(
                "This folder's filesystem doesn't report consistent file "
                "identities, so Sortl can't verify moves safely."
            )
    finally:
        for path in reversed(created):
            with suppress(OSError):
                path.unlink()


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Everything the UI needs to describe a finished (or stopped) run."""

    root: Path
    results: tuple[ExecutionResult, ...]
    skipped_conflicts: tuple[PlannedFile, ...]
    planning_errors: tuple[PlanningError, ...]
    total: int
    cancelled: bool = False
    halted: bool = False

    def _count(self, status: ExecutionStatus) -> int:
        return sum(result.status == status for result in self.results)

    @property
    def completed(self) -> int:
        return self._count(ExecutionStatus.COMPLETED)

    @property
    def blocked(self) -> int:
        return self._count(ExecutionStatus.BLOCKED)

    @property
    def recovery_required(self) -> int:
        return self._count(ExecutionStatus.RECOVERY_REQUIRED)

    @property
    def not_attempted(self) -> int:
        return self.total - len(self.results)

    @property
    def categories_used(self) -> int:
        return len(
            {
                result.destination.parent
                for result in self.results
                if result.status == ExecutionStatus.COMPLETED
            }
        )


def run_plan(
    plan: Plan,
    journal_path: Path,
    *,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> RunSummary:
    """Execute a plan one file at a time through the core executor.

    Running one file per ``execute_plan`` call uses only the core's public
    API while giving us progress reporting and cancellation between files.

    Policy, chosen to fail safe:
    * conflicts are skipped, never overwritten;
    * a ``BLOCKED`` file (changed since planning, etc.) is reported and the
      run continues, because nothing was modified;
    * the first ``RECOVERY_REQUIRED`` halts the whole run, because something
      unexpected happened to the filesystem and continuing could compound it.

    Raises ``FolderCheckError`` before touching anything if the folder is not
    writable or lacks hard-link support.
    """
    probe_hardlink_support(plan.root)

    journal_path = Path(journal_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    to_move = [op for op in plan.operations if not op.conflict]
    skipped = tuple(op for op in plan.operations if op.conflict)
    results: list[ExecutionResult] = []
    cancelled = False
    halted = False

    with OperationJournal(journal_path) as journal:
        for index, operation in enumerate(to_move, start=1):
            if cancel is not None and cancel.is_set():
                cancelled = True
                break

            single = Plan(root=plan.root, operations=(operation,), errors=())
            report = execute_plan(single, journal, dry_run=False)
            results.extend(report.results)

            if progress is not None:
                progress(index, len(to_move), operation.source)

            if any(
                result.status == ExecutionStatus.RECOVERY_REQUIRED
                for result in report.results
            ):
                halted = True
                break

    return RunSummary(
        root=plan.root,
        results=tuple(results),
        skipped_conflicts=skipped,
        planning_errors=plan.errors,
        total=len(to_move),
        cancelled=cancelled,
        halted=halted,
    )


def load_history(journal_path: Path) -> tuple[JournalEntry, ...]:
    """Return journal entries, newest first. Cheap: no files are inspected."""
    journal_path = Path(journal_path)

    if not journal_path.exists():
        return ()

    with OperationJournal(journal_path) as journal:
        entries = journal.list_entries()

    return tuple(sorted(entries, key=lambda e: e.created_at, reverse=True))


def count_needing_attention(entries: Iterable[JournalEntry]) -> int:
    return sum(entry.status in ATTENTION_STATUSES for entry in entries)


def inspect_history(journal_path: Path) -> dict[str, RecoveryFinding]:
    """Run the read-only recovery inspector; returns findings by operation id.

    This re-hashes files for every journal entry, so it is on-demand only.
    """
    journal_path = Path(journal_path)

    if not journal_path.exists():
        return {}

    with OperationJournal(journal_path) as journal:
        report = inspect_journal(journal)

    return {finding.operation_id: finding for finding in report.findings}
