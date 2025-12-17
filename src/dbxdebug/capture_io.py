"""
Capture file I/O utilities for .capture.gz format.

The .capture.gz format is a gzip-compressed pickle file containing
timestamped VGA screen captures.
"""

import gzip
import pickle
from pathlib import Path
from typing import Any


def load_capture(filepath: str | Path) -> Any:
    """
    Load a capture file (.capture.gz or legacy .pickle).

    Automatically detects format based on extension.

    Args:
        filepath: Path to the capture file

    Returns:
        The unpickled capture data
    """
    filepath = Path(filepath)

    if filepath.suffix == ".gz" or filepath.name.endswith(".capture.gz"):
        with gzip.open(filepath, "rb") as f:
            return pickle.load(f)
    else:
        # Legacy .pickle format
        with open(filepath, "rb") as f:
            return pickle.load(f)


def save_capture(data: Any, filepath: str | Path) -> None:
    """
    Save capture data to a .capture.gz file.

    Args:
        data: The capture data to save
        filepath: Output path (will use .capture.gz extension)
    """
    filepath = Path(filepath)

    # Ensure .capture.gz extension
    if not filepath.name.endswith(".capture.gz"):
        if filepath.suffix in (".pickle", ".pkl", ".gz"):
            filepath = filepath.with_suffix(".capture.gz")
        else:
            filepath = Path(str(filepath) + ".capture.gz")

    with gzip.open(filepath, "wb") as f:
        pickle.dump(data, f)


def get_capture_path(base_name: str, output_dir: str | Path = ".") -> Path:
    """
    Generate a capture file path with proper extension.

    Args:
        base_name: Base name for the file (without extension)
        output_dir: Output directory

    Returns:
        Path object for the capture file
    """
    output_dir = Path(output_dir)

    # Remove any existing extension
    base_name = base_name.replace(".pickle", "").replace(".capture.gz", "")

    return output_dir / f"{base_name}.capture.gz"
