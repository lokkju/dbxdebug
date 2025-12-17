"""
Capture file I/O utilities for .capture.gz format.

The .capture.gz format is a gzip-compressed pickle file containing
timestamped VGA screen captures.
"""

import gzip
import pickle
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .video import DOSVideoTools


class ScreenRecorder:
    """
    Records screen captures with timestamps.

    Usage:
        with DOSVideoTools() as video:
            recorder = ScreenRecorder()

            # Manual capture
            recorder.capture(video)

            # Or timed recording
            recorder.record(video, duration=10.0, sample_rate=50)

            # Save
            recorder.save("session.capture.gz")
    """

    def __init__(self, metadata: dict | None = None):
        """
        Initialize a new recorder.

        Args:
            metadata: Optional dict of metadata to include in capture file
        """
        self.screens: dict[int, list[str]] = {}
        self.metadata: dict = metadata or {}

    def capture(self, video: "DOSVideoTools") -> bool:
        """
        Capture a single screen frame with current timestamp.

        Args:
            video: DOSVideoTools instance

        Returns:
            True if capture succeeded
        """
        timestamp_ns = time.time_ns()
        screen = video.screen_dump()
        if screen is not None:
            self.screens[timestamp_ns] = screen
            return True
        return False

    def capture_raw(self, video: "DOSVideoTools") -> bool:
        """
        Capture raw video memory (with attributes) with current timestamp.

        Args:
            video: DOSVideoTools instance

        Returns:
            True if capture succeeded
        """
        timestamp_ns = time.time_ns()
        raw = video.screen_raw()
        if raw is not None:
            self.screens[timestamp_ns] = raw  # type: ignore
            return True
        return False

    def record(
        self,
        video: "DOSVideoTools",
        duration: float,
        sample_rate: float = 50.0,
        raw: bool = False,
    ) -> int:
        """
        Record screens for a duration at specified sample rate.

        Args:
            video: DOSVideoTools instance
            duration: Recording duration in seconds
            sample_rate: Samples per second (Hz)
            raw: If True, capture raw bytes instead of text

        Returns:
            Number of frames captured
        """
        sample_interval = 1.0 / sample_rate
        total_samples = int(duration * sample_rate)
        start_time = time.time()
        captured = 0

        capture_fn = self.capture_raw if raw else self.capture

        for i in range(total_samples):
            if capture_fn(video):
                captured += 1

            # Sleep until next sample time
            elapsed = time.time() - start_time
            next_sample_time = (i + 1) * sample_interval
            sleep_time = next_sample_time - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        self.metadata["duration"] = duration
        self.metadata["sample_rate"] = sample_rate
        return captured

    def save(self, filepath: str | Path) -> None:
        """Save captured screens to file."""
        data = {
            "screens": self.screens,
            **self.metadata,
        }
        save_capture(data, filepath)

    def clear(self) -> None:
        """Clear all captured screens."""
        self.screens.clear()

    def __len__(self) -> int:
        return len(self.screens)

    @property
    def timestamps(self) -> list[int]:
        """Get sorted list of capture timestamps (ns)."""
        return sorted(self.screens.keys())

    @property
    def duration_seconds(self) -> float:
        """Get duration of capture in seconds."""
        if len(self.screens) < 2:
            return 0.0
        ts = self.timestamps
        return (ts[-1] - ts[0]) / 1_000_000_000


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
