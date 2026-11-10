from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    shared_root = Path(__file__).resolve().parents[3] / "shared"
    if str(shared_root) not in sys.path:
        sys.path.insert(0, str(shared_root))

from .api import create_app

__all__ = ["create_app"]
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    shared_root = Path(__file__).resolve().parents[3] / "shared"
    if str(shared_root) not in sys.path:
        sys.path.insert(0, str(shared_root))
