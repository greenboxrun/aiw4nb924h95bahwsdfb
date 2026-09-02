"""Repository paths shared by production modules."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_ROOT = PROJECT_ROOT / "result"
