__all__ = ["__version__"]

__version__ = "1.2.0"
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    shared_root = Path(__file__).resolve().parents[3] / "shared"
    if str(shared_root) not in sys.path:
        sys.path.insert(0, str(shared_root))
