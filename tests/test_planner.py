from hashlib import sha256
from pathlib import Path

import pytest

from sortl.planner import create_plan


def test_classifies_files_and_calculates_hashes(tmp_path: Path):
    image = tmp_path / "photo.PNG"
    image.write_bytes(b"image data")

    plan = create_plan(tmp_path)

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.source == image
    assert operation.destination == tmp_path / "Images" / "photo.PNG"
    assert operation.sha256 == sha256(b"image data").hexdigest()
    assert operation.conflict is False
    assert plan.errors == ()


def test_unknown_extension_uses_other_category(tmp_path: Path):
    file = tmp_path / "data.xyz"
    file.write_text("sample")

    plan = create_plan(tmp_path)

    assert plan.operations[0].destination == tmp_path / "Other" / "data.xyz"


def test_extensionless_file_uses_other_category(tmp_path: Path):
    file = tmp_path / "README"
    file.write_text("sample")

    plan = create_plan(tmp_path)

    assert plan.operations[0].destination == tmp_path / "Other" / "README"


def test_existing_destination_is_reported_as_conflict(tmp_path: Path):
    source = tmp_path / "photo.png"
    source.write_bytes(b"source")

    destination_dir = tmp_path / "Images"
    destination_dir.mkdir()
    destination = destination_dir / source.name
    destination.write_bytes(b"existing destination")

    plan = create_plan(tmp_path)

    operation = next(item for item in plan.operations if item.source == source)
    assert operation.conflict is True
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"existing destination"


def test_planning_does_not_create_destination_directories(tmp_path: Path):
    (tmp_path / "report.pdf").write_text("report")

    create_plan(tmp_path)

    assert not (tmp_path / "Documents").exists()


def test_only_scans_top_level_regular_files(tmp_path: Path):
    (tmp_path / "top.txt").write_text("top")

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside")

    plan = create_plan(tmp_path)

    assert [item.source.name for item in plan.operations] == ["top.txt"]


def test_symlink_is_skipped(tmp_path: Path):
    target = tmp_path / "target.txt"
    target.write_text("target")

    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is not permitted in this environment")

    plan = create_plan(tmp_path)

    assert {item.source.name for item in plan.operations} == {"target.txt"}


def test_missing_directory_is_rejected(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        create_plan(tmp_path / "missing")


def test_file_path_is_rejected(tmp_path: Path):
    file = tmp_path / "file.txt"
    file.write_text("content")

    with pytest.raises(NotADirectoryError):
        create_plan(file)


def test_plan_order_is_deterministic(tmp_path: Path):
    (tmp_path / "z.txt").write_text("z")
    (tmp_path / "A.txt").write_text("a")
    (tmp_path / "m.txt").write_text("m")

    plan = create_plan(tmp_path)

    assert [item.source.name for item in plan.operations] == [
        "A.txt",
        "m.txt",
        "z.txt",
    ]
