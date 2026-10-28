"""Core data models and lifecycle rules for Sortl file operations."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class OperationStatus(StrEnum):
    """Represent the lifecycle states of a file operation."""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


_ALLOWED_TRANSITIONS = {
    OperationStatus.PLANNED: {
        OperationStatus.IN_PROGRESS,
    },
    OperationStatus.IN_PROGRESS: {
        OperationStatus.COMPLETED,
        OperationStatus.RECOVERY_REQUIRED,
    },
    OperationStatus.COMPLETED: {
        OperationStatus.ROLLED_BACK,
        OperationStatus.RECOVERY_REQUIRED,
    },
    OperationStatus.RECOVERY_REQUIRED: {
        OperationStatus.COMPLETED,
        OperationStatus.ROLLED_BACK,
    },
    OperationStatus.ROLLED_BACK: set(),
}


@dataclass(slots=True)
class FileOperation:
    """Represent a file operation and its current execution status.

    Attributes:
        source: Original path of the file.
        destination: Intended destination path.
        sha256: Expected SHA-256 digest of the file's contents.
        operation_id: Unique identifier used to track the operation.
        status: Current lifecycle status of the operation.
    """

    source: Path
    destination: Path
    sha256: str
    operation_id: str
    status: OperationStatus = OperationStatus.PLANNED

    @classmethod
    def create(
        cls,
        source: Path,
        destination: Path,
        sha256: str,
    ) -> FileOperation:
        """Create a new file operation with validated initial values.

        Generates a unique operation ID and initializes the status to
        PLANNED. The supplied hash is normalized to lowercase.

        Args:
            source: Original path of the file.
            destination: Intended destination path.
            sha256: Expected 64-character hexadecimal SHA-256 digest.

        Returns:
            A new FileOperation instance.

        Raises:
            ValueError: If the hash is invalid or the source and destination
                paths are identical.
        """
        if not sha256 or len(sha256) != 64:
            raise ValueError("sha256 must be a 64-character hexadecimal digest")

        try:
            int(sha256, 16)
        except ValueError as exc:
            raise ValueError("sha256 must be hexadecimal") from exc

        if source == destination:
            raise ValueError("Source and destination must differ")

        return cls(
            source=source,
            destination=destination,
            sha256=sha256.lower(),
            operation_id=str(uuid4()),
        )

    def transition_to(self, new_status: OperationStatus) -> None:
        """Change the operation's status if the transition is permitted.

        Valid transitions are defined by _ALLOWED_TRANSITIONS. The current
        status remains unchanged if the requested transition is invalid.

        Args:
            new_status: Lifecycle status to transition to.

        Raises:
            ValueError: If the requested status transition is not allowed.
        """
        allowed = _ALLOWED_TRANSITIONS[self.status]

        if new_status not in allowed:
            raise ValueError(
                f"Invalid operation transition: "
                f"{self.status.value} -> {new_status.value}"
            )

        self.status = new_status