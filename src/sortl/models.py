from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class OperationStatus(StrEnum):
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
        allowed = _ALLOWED_TRANSITIONS[self.status]

        if new_status not in allowed:
            raise ValueError(
                f"Invalid operation transition: "
                f"{self.status.value} -> {new_status.value}"
            )

        self.status = new_status