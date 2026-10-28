from pathlib import Path

import pytest

from sortl.models import FileOperation, OperationStatus


VALID_HASH = "a" * 64


def make_operation() -> FileOperation:
    return FileOperation.create(
        source=Path("/tmp/source.txt"),
        destination=Path("/tmp/organized/source.txt"),
        sha256=VALID_HASH,
    )


def test_new_operation_starts_planned():
    operation = make_operation()

    assert operation.status == OperationStatus.PLANNED
    assert operation.operation_id


def test_valid_transition_to_in_progress():
    operation = make_operation()

    operation.transition_to(OperationStatus.IN_PROGRESS)

    assert operation.status == OperationStatus.IN_PROGRESS


def test_invalid_transition_is_rejected():
    operation = make_operation()

    with pytest.raises(ValueError, match="Invalid operation transition"):
        operation.transition_to(OperationStatus.COMPLETED)


def test_completed_operation_can_be_rolled_back():
    operation = make_operation()
    operation.transition_to(OperationStatus.IN_PROGRESS)
    operation.transition_to(OperationStatus.COMPLETED)
    operation.transition_to(OperationStatus.ROLLED_BACK)

    assert operation.status == OperationStatus.ROLLED_BACK


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "abc",
        "g" * 64,
        "a" * 63,
        "a" * 65,
    ],
)
def test_invalid_hash_is_rejected(digest: str):
    with pytest.raises(ValueError):
        FileOperation.create(
            source=Path("/tmp/source.txt"),
            destination=Path("/tmp/organized/source.txt"),
            sha256=digest,
        )


def test_identical_paths_are_rejected():
    with pytest.raises(ValueError, match="must differ"):
        FileOperation.create(
            source=Path("/tmp/source.txt"),
            destination=Path("/tmp/source.txt"),
            sha256=VALID_HASH,
        )