from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

FILE_CATEGORIES: dict[str, str] = {
    ".jpg": "Images",
    ".jpeg": "Images",
    ".png": "Images",
    ".gif": "Images",
    ".webp": "Images",
    ".svg": "Images",
    ".pdf": "Documents",
    ".doc": "Documents",
    ".docx": "Documents",
    ".txt": "Documents",
    ".odt": "Documents",
    ".rtf": "Documents",
    ".csv": "Spreadsheets",
    ".xls": "Spreadsheets",
    ".xlsx": "Spreadsheets",
    ".ods": "Spreadsheets",
    ".zip": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
    ".7z": "Archives",
    ".rar": "Archives",
    ".mp3": "Audio",
    ".wav": "Audio",
    ".flac": "Audio",
    ".aac": "Audio",
    ".ogg": "Audio",
    ".mp4": "Videos",
    ".mkv": "Videos",
    ".mov": "Videos",
    ".avi": "Videos",
    ".webm": "Videos",
    ".py": "Code",
    ".js": "Code",
    ".ts": "Code",
    ".java": "Code",
    ".c": "Code",
    ".cpp": "Code",
    ".html": "Code",
    ".css": "Code",
}


@dataclass(frozen=True, slots=True)
class PlannedFile:
    source: Path
    destination: Path
    sha256: str
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class PlanningError:
    source: Path
    message: str


@dataclass(frozen=True, slots=True)
class Plan:
    root: Path
    operations: tuple[PlannedFile, ...]
    errors: tuple[PlanningError, ...]


def _hash_file(path: Path) -> str:
    """Calculate the SHA-256 hash of a file using chunked reads.
    
        Reads the file in 1 MiB chunks to avoid loading the entire file
        into memory.
    
        Args:
            path: Path to the file whose content should be hashed.
    
        Returns:
            The file's SHA-256 digest as a 64-character hexadecimal string.
    
        Raises:
            OSError: If the file cannot be opened or read.
        """
    digest = sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _category_for(path: Path) -> str:
    return FILE_CATEGORIES.get(path.suffix.lower(), "Other")


def create_plan(root: Path) -> Plan:
    """Build a read-only plan for regular files directly inside root."""
    root = Path(root).expanduser()

    if not root.exists():
        raise FileNotFoundError(f"Directory does not exist: {root}")

    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    root = root.resolve()
    operations: list[PlannedFile] = []
    errors: list[PlanningError] = []

    # Sort for deterministic plans and easier testing.
    for source in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        # Do not follow symlinks or process directories.
        if source.is_symlink() or not source.is_file():
            continue

        destination = root / _category_for(source) / source.name

        try:
            file_hash = _hash_file(source)
        except OSError as exc:
            errors.append(PlanningError(source, str(exc)))
            continue

        operations.append(
            PlannedFile(
                source=source,
                destination=destination,
                sha256=file_hash,
                conflict=destination.exists(),
            )
        )

    return Plan(
        root=root,
        operations=tuple(operations),
        errors=tuple(errors),
    )
