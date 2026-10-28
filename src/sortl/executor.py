from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sortl.journal import OperationJournal
from sortl.models import FileOperation, OperationStatus
from sortl.planner import Plan, PlannedFile, PlanningError


class UnsafeOperationError(RuntimeError):
    """Raised when a file operation fails a safety check."""


class ExecutionStatus(StrEnum):
    """Outcome reported by the executor."""

    DRY_RUN = "dry_run"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome for one planned file."""

    source: Path
    destination: Path
    status: ExecutionStatus
    message: str
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Results for a plan, including errors recorded by the planner."""

    dry_run: bool
    results: tuple[ExecutionResult, ...]
    planning_errors: tuple[PlanningError, ...]

    @property
    def completed_count(self) -> int:
        """Return the number of operations completed successfully."""
        return sum(
            result.status == ExecutionStatus.COMPLETED
            for result in self.results
        )


def _identity(info: os.stat_result) -> tuple[int, int]:
    """Return the device and inode identifying a filesystem object."""
    return info.st_dev, info.st_ino


def _hash_regular_file(path: Path) -> tuple[str, tuple[int, int]]:
    """Hash a regular file without following a final-component symlink."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeOperationError(f"Not a regular file: {path}")

        digest = hashlib.sha256()

        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)

        after = os.fstat(descriptor)

        before_metadata = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_metadata = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )

        if before_metadata != after_metadata:
            raise UnsafeOperationError(
                f"File changed while being hashed: {path}"
            )

        path_info = os.lstat(path)

        if (
            not stat.S_ISREG(path_info.st_mode)
            or _identity(path_info) != _identity(after)
        ):
            raise UnsafeOperationError(
                f"File path changed while being hashed: {path}"
            )

        return digest.hexdigest(), _identity(after)

    finally:
        os.close(descriptor)


def _validate_destination_parent(root: Path, parent: Path) -> None:
    """Ensure the destination directory is a direct, safe child of root."""
    if parent == root or parent.parent != root:
        raise UnsafeOperationError(
            f"Destination directory is outside the expected layout: {parent}"
        )

    if not os.path.lexists(parent):
        return

    info = os.lstat(parent)

    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeOperationError(
            "Destination directory must be a real directory, not a symlink: "
            f"{parent}"
        )

    if parent.resolve(strict=True).parent != root:
        raise UnsafeOperationError(
            f"Destination directory resolves outside the root: {parent}"
        )


def _preflight(root: Path, planned: PlannedFile) -> tuple[int, int]:
    """Validate a planned operation without changing the filesystem."""
    root = root.resolve(strict=True)

    if not root.is_dir():
        raise UnsafeOperationError(f"Not a directory: {root}")

    source = planned.source
    destination = planned.destination

    if not source.is_absolute() or not destination.is_absolute():
        raise UnsafeOperationError("Source and destination must be absolute")

    if source.parent != root:
        raise UnsafeOperationError(
            f"Source must be directly inside the planned root: {source}"
        )

    if (
        destination.parent.parent != root
        or destination.parent == root
        or destination.name != source.name
    ):
        raise UnsafeOperationError(
            "Destination does not match the expected folder layout: "
            f"{destination}"
        )

    if planned.conflict:
        raise UnsafeOperationError(
            "The plan recorded a destination conflict; create a fresh plan"
        )

    if os.path.lexists(destination):
        raise UnsafeOperationError(
            f"Destination already exists: {destination}"
        )

    _validate_destination_parent(root, destination.parent)

    actual_hash, identity = _hash_regular_file(source)

    if actual_hash != planned.sha256:
        raise UnsafeOperationError(
            f"Source content changed since planning: {source}"
        )

    return identity


def _ensure_destination_parent(root: Path, destination: Path) -> None:
    """Create the required category directory and validate it."""
    with suppress(FileExistsError):
        destination.parent.mkdir()

    _validate_destination_parent(root, destination.parent)


def _perform_move(
    root: Path,
    planned: PlannedFile,
    original_identity: tuple[int, int],
) -> None:
    """Create a non-overwriting hard link, then remove the source name.

    This strategy requires a filesystem that supports hard links.
    There is deliberately no fallback to an overwriting move or copy.
    """
    _ensure_destination_parent(root, planned.destination)

    current_identity = _preflight(root, planned)

    if current_identity != original_identity:
        raise UnsafeOperationError(
            f"Source identity changed before execution: {planned.source}"
        )

    # Link creation fails if the destination already exists.
    os.link(
        planned.source,
        planned.destination,
        follow_symlinks=False,
    )

    _validate_destination_parent(root, planned.destination.parent)

    destination_hash, destination_identity = _hash_regular_file(
        planned.destination
    )
    source_info = os.lstat(planned.source)

    if (
        not stat.S_ISREG(source_info.st_mode)
        or _identity(source_info) != original_identity
        or destination_identity != original_identity
    ):
        raise UnsafeOperationError(
            "Source or destination identity changed during the move"
        )

    if destination_hash != planned.sha256:
        raise UnsafeOperationError(
            "Destination hash differs from the planned source hash"
        )

    # Recheck identities immediately before removing the source path.
    source_info = os.lstat(planned.source)
    destination_info = os.lstat(planned.destination)

    if (
        not stat.S_ISREG(source_info.st_mode)
        or _identity(source_info) != original_identity
        or _identity(destination_info) != original_identity
    ):
        raise UnsafeOperationError(
            "File identity changed before removing the source"
        )

    planned.source.unlink()

    if os.path.lexists(planned.source):
        raise UnsafeOperationError(
            f"Source still exists after the move: {planned.source}"
        )

    final_hash, final_identity = _hash_regular_file(planned.destination)

    if (
        final_hash != planned.sha256
        or final_identity != original_identity
    ):
        raise UnsafeOperationError(
            "Destination failed verification after the move"
        )


def _mark_recovery_required(
    journal: OperationJournal,
    operation_id: str,
    reason: str,
) -> str:
    """Try to persist recovery-required without hiding expected failures."""
    try:
        entry = journal.get(operation_id)

        if entry is None:
            return "Journal entry could not be found."

        if entry.status == OperationStatus.RECOVERY_REQUIRED:
            return ""

        if entry.status not in {
            OperationStatus.IN_PROGRESS,
            OperationStatus.COMPLETED,
        }:
            return (
                f"Journal remains in state {entry.status.value}; "
                "inspect it before retrying."
            )

        journal.transition(
            operation_id,
            OperationStatus.RECOVERY_REQUIRED,
            error=reason,
        )

    except (
        OSError,
        sqlite3.Error,
        ValueError,
        RuntimeError,
        TypeError,
    ) as exc:
        return f"Could not update the journal: {exc}"

    return ""


def _execute_one(
    root: Path,
    planned: PlannedFile,
    journal: OperationJournal,
    *,
    dry_run: bool,
) -> ExecutionResult:
    """Validate and, when permitted, execute one planned operation."""
    try:
        original_identity = _preflight(root, planned)

    except (OSError, RuntimeError, ValueError) as exc:
        return ExecutionResult(
            source=planned.source,
            destination=planned.destination,
            status=ExecutionStatus.BLOCKED,
            message=f"Preflight failed: {exc}",
        )

    if dry_run:
        return ExecutionResult(
            source=planned.source,
            destination=planned.destination,
            status=ExecutionStatus.DRY_RUN,
            message="Checks passed; no files or journal records were changed.",
        )

    try:
        operation = FileOperation.create(
            source=planned.source,
            destination=planned.destination,
            sha256=planned.sha256,
        )
        entry = journal.create(operation)

    except (
        OSError,
        sqlite3.Error,
        ValueError,
        RuntimeError,
        TypeError,
    ) as exc:
        return ExecutionResult(
            source=planned.source,
            destination=planned.destination,
            status=ExecutionStatus.BLOCKED,
            message=(
                "Could not persist the operation; no move was attempted: "
                f"{exc}"
            ),
        )

    try:
        journal.transition(
            entry.operation_id,
            OperationStatus.IN_PROGRESS,
        )

    except (
        OSError,
        sqlite3.Error,
        ValueError,
        RuntimeError,
        TypeError,
    ) as exc:
        return ExecutionResult(
            source=planned.source,
            destination=planned.destination,
            status=ExecutionStatus.BLOCKED,
            message=(
                "Could not confirm the in-progress journal state. "
                "No move was attempted; inspect the journal before retrying. "
                f"Details: {exc}"
            ),
            operation_id=entry.operation_id,
        )

    try:
        _perform_move(root, planned, original_identity)

        journal.transition(
            entry.operation_id,
            OperationStatus.COMPLETED,
        )

    except (
        OSError,
        sqlite3.Error,
        ValueError,
        RuntimeError,
        TypeError,
    ) as exc:
        reason = f"{type(exc).__name__}: {exc}"

        journal_note = _mark_recovery_required(
            journal,
            entry.operation_id,
            reason,
        )

        message = (
            "Execution failed or could not be verified. Inspect the source, "
            "destination, and journal before retrying. "
            f"Details: {reason}"
        )

        if journal_note:
            message = f"{message} Journal note: {journal_note}"

        return ExecutionResult(
            source=planned.source,
            destination=planned.destination,
            status=ExecutionStatus.RECOVERY_REQUIRED,
            message=message,
            operation_id=entry.operation_id,
        )

    return ExecutionResult(
        source=planned.source,
        destination=planned.destination,
        status=ExecutionStatus.COMPLETED,
        message="Move completed and the destination hash was verified.",
        operation_id=entry.operation_id,
    )


def execute_plan(
    plan: Plan,
    journal: OperationJournal,
    *,
    dry_run: bool = True,
) -> ExecutionReport:
    """Validate a plan and execute it, using dry-run by default.

    Dry-run performs read-only preflight checks. It neither creates category
    directories nor writes to the journal.
    """
    results = tuple(
        _execute_one(
            plan.root,
            planned,
            journal,
            dry_run=dry_run,
        )
        for planned in plan.operations
    )

    return ExecutionReport(
        dry_run=dry_run,
        results=results,
        planning_errors=plan.errors,
    )