from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sortl.journal import JournalEntry, OperationJournal
from sortl.models import OperationStatus


class RecoveryAssessment(StrEnum):
    """Read-only assessment of a journal entry and its filesystem state."""

    NOT_STARTED = "not_started"
    SOURCE_INTACT = "source_intact"
    LINKED_BOTH_NAMES = "linked_both_names"
    MOVE_MAY_HAVE_COMPLETED = "move_may_have_completed"
    COMPLETED_CONSISTENT = "completed_consistent"
    ROLLED_BACK_CONSISTENT = "rolled_back_consistent"
    SOURCE_CHANGED = "source_changed"
    DESTINATION_HASH_MISMATCH = "destination_hash_mismatch"
    DESTINATION_CONFLICT = "destination_conflict"
    FILES_MISSING = "files_missing"
    UNSAFE_PATH = "unsafe_path"
    INSPECTION_ERROR = "inspection_error"
    UNEXPECTED_STATE = "unexpected_state"


@dataclass(frozen=True, slots=True)
class RecoveryFinding:
    """Read-only finding for one journal entry."""

    operation_id: str
    source: Path
    destination: Path
    journal_status: OperationStatus
    assessment: RecoveryAssessment
    message: str
    source_exists: bool
    destination_exists: bool
    source_sha256: str | None
    destination_sha256: str | None


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Collection of recovery findings in journal order."""

    findings: tuple[RecoveryFinding, ...]


@dataclass(frozen=True, slots=True)
class _Observation:
    """Snapshot of a path collected without modifying the filesystem."""

    exists: bool
    sha256: str | None = None
    identity: tuple[int, int] | None = None
    error: str | None = None
    unsafe: bool = False


def _identity(info: os.stat_result) -> tuple[int, int]:
    """Return the device and inode identifying a filesystem object."""
    return info.st_dev, info.st_ino


def _metadata_key(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return metadata used to detect changes during inspection."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _parent_issue(path: Path) -> tuple[str | None, bool]:
    """Detect unsafe or inaccessible parent directories without following them."""
    for parent in reversed(path.parents):
        try:
            info = os.lstat(parent)
        except FileNotFoundError:
            return None, False
        except OSError as exc:
            return f"Cannot inspect parent {parent}: {exc}", False

        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return (
                f"Parent path is not a real directory: {parent}",
                True,
            )

    return None, False


def _observe(path: Path) -> _Observation:
    """Inspect a path, refusing symlinks and non-regular files."""
    parent_error, unsafe_parent = _parent_issue(path)

    if parent_error is not None:
        return _Observation(
            exists=True,
            error=parent_error,
            unsafe=unsafe_parent,
        )

    if not os.path.lexists(path):
        return _Observation(exists=False)

    descriptor: int | None = None

    try:
        path_info = os.lstat(path)

        if not stat.S_ISREG(path_info.st_mode):
            return _Observation(
                exists=True,
                error="Path is a symlink or is not a regular file.",
                unsafe=True,
            )

        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)

        before = os.fstat(descriptor)

        if (
            not stat.S_ISREG(before.st_mode)
            or _identity(before) != _identity(path_info)
        ):
            return _Observation(
                exists=True,
                error="File identity changed before inspection.",
                unsafe=True,
            )

        digest = hashlib.sha256()

        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)

        after = os.fstat(descriptor)
        current_path_info = os.lstat(path)

        if (
            _metadata_key(before) != _metadata_key(after)
            or not stat.S_ISREG(current_path_info.st_mode)
            or _identity(current_path_info) != _identity(after)
        ):
            return _Observation(
                exists=True,
                error="File changed during inspection.",
                unsafe=True,
            )

        return _Observation(
            exists=True,
            sha256=digest.hexdigest(),
            identity=_identity(after),
        )

    except OSError as exc:
        return _Observation(
            exists=True,
            error=f"{type(exc).__name__}: {exc}",
        )

    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _classify(
    entry: JournalEntry,
    source: _Observation,
    destination: _Observation,
) -> tuple[RecoveryAssessment, str]:
    """Classify the observed state without modifying anything."""
    if source.unsafe or destination.unsafe:
        return (
            RecoveryAssessment.UNSAFE_PATH,
            (
                "A symlink, non-regular file, or changed filesystem identity "
                "was detected. Do not perform automatic recovery."
            ),
        )

    if source.error or destination.error:
        details = "; ".join(
            error
            for error in (source.error, destination.error)
            if error is not None
        )
        return (
            RecoveryAssessment.INSPECTION_ERROR,
            f"The filesystem state could not be verified: {details}",
        )

    if not source.exists and not destination.exists:
        return (
            RecoveryAssessment.FILES_MISSING,
            (
                "Neither source nor destination exists. The file may have "
                "been removed or moved elsewhere; investigate before "
                "changing the journal."
            ),
        )

    if source.exists and destination.exists:
        if source.identity != destination.identity:
            return (
                RecoveryAssessment.DESTINATION_CONFLICT,
                (
                    "Both paths exist but refer to different filesystem "
                    "objects. Do not overwrite or remove either file "
                    "automatically."
                ),
            )

        if source.sha256 != entry.sha256:
            return (
                RecoveryAssessment.SOURCE_CHANGED,
                (
                    "Both names refer to the same filesystem object, but "
                    "its content does not match the recorded hash."
                ),
            )

        if destination.sha256 != entry.sha256:
            return (
                RecoveryAssessment.DESTINATION_HASH_MISMATCH,
                (
                    "The destination hash differs from the recorded hash. "
                    "Manual inspection is required."
                ),
            )

        if entry.status in {
            OperationStatus.IN_PROGRESS,
            OperationStatus.RECOVERY_REQUIRED,
        }:
            return (
                RecoveryAssessment.LINKED_BOTH_NAMES,
                (
                    "Both paths refer to the same file and its hash matches. "
                    "This may be an interruption after link creation but "
                    "before source removal. Neither path has been changed "
                    "by this inspection."
                ),
            )

        return (
            RecoveryAssessment.UNEXPECTED_STATE,
            (
                f"Both paths exist, but journal status is "
                f"{entry.status.value}. Inspect the operation history and "
                "both paths manually."
            ),
        )

    if source.exists:
        if source.sha256 != entry.sha256:
            return (
                RecoveryAssessment.SOURCE_CHANGED,
                (
                    "The source exists, but its hash differs from the "
                    "planned hash."
                ),
            )

        if entry.status == OperationStatus.PLANNED:
            return (
                RecoveryAssessment.NOT_STARTED,
                (
                    "The source matches the planned hash and the destination "
                    "is absent. No move is visible in the current filesystem "
                    "state."
                ),
            )

        if entry.status == OperationStatus.ROLLED_BACK:
            return (
                RecoveryAssessment.ROLLED_BACK_CONSISTENT,
                (
                    "The source matches the recorded hash, the destination "
                    "is absent, and the journal says the operation was "
                    "rolled back."
                ),
            )

        if entry.status in {
            OperationStatus.IN_PROGRESS,
            OperationStatus.RECOVERY_REQUIRED,
        }:
            return (
                RecoveryAssessment.SOURCE_INTACT,
                (
                    "The source matches the recorded hash and the destination "
                    "is absent. The move is not visible, but inspect the "
                    "journal before deciding whether to retry."
                ),
            )

        return (
            RecoveryAssessment.UNEXPECTED_STATE,
            (
                "The source exists and the destination is absent, which "
                f"conflicts with journal status {entry.status.value}."
            ),
        )

    # The source is absent and the destination exists.
    if destination.sha256 != entry.sha256:
        return (
            RecoveryAssessment.DESTINATION_HASH_MISMATCH,
            (
                "The source is absent, and the destination does not match "
                "the recorded hash. Do not mark this operation completed."
            ),
        )

    if entry.status == OperationStatus.COMPLETED:
        return (
            RecoveryAssessment.COMPLETED_CONSISTENT,
            (
                "The source is absent and the destination matches the "
                "recorded hash, consistent with the completed journal status."
            ),
        )

    if entry.status in {
        OperationStatus.IN_PROGRESS,
        OperationStatus.RECOVERY_REQUIRED,
    }:
        return (
            RecoveryAssessment.MOVE_MAY_HAVE_COMPLETED,
            (
                "The source is absent and the destination hash matches. "
                "The move may have succeeded before the journal was updated, "
                "but the record alone cannot prove that Sortl performed it."
            ),
        )

    return (
        RecoveryAssessment.UNEXPECTED_STATE,
        (
            "The source is absent and the destination matches the recorded "
            f"hash, but journal status is {entry.status.value}. "
            "Manual review is required."
        ),
    )


def _inspect_entry(entry: JournalEntry) -> RecoveryFinding:
    """Inspect both paths for one journal entry."""
    source = _observe(entry.source)
    destination = _observe(entry.destination)
    assessment, message = _classify(entry, source, destination)

    return RecoveryFinding(
        operation_id=entry.operation_id,
        source=entry.source,
        destination=entry.destination,
        journal_status=entry.status,
        assessment=assessment,
        message=message,
        source_exists=source.exists,
        destination_exists=destination.exists,
        source_sha256=source.sha256,
        destination_sha256=destination.sha256,
    )


def inspect_journal(journal: OperationJournal) -> RecoveryReport:
    """Inspect all journal entries without modifying files or database rows."""
    return RecoveryReport(
        findings=tuple(
            _inspect_entry(entry)
            for entry in journal.list_entries()
        )
    )