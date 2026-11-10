"""Run suites independently because every component owns a package called app."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    "coordinator": "coordinator/blender_render_farm_coordinator",
    "worker": "worker/blender_render_farm_worker",
    "master": "master/blender_render_farm_master",
    "viewer": "image_viewer/BlenderImageViewer",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("components", nargs="*", help="Names: " + ", ".join(COMPONENTS))
    args = parser.parse_args()
    names = args.components or list(COMPONENTS)
    invalid = set(names) - COMPONENTS.keys()
    if invalid:
        parser.error("Unknown components: " + ", ".join(sorted(invalid)))
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    failed = []
    for name in names:
        print(f"\nRunning {name} tests", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q"],
            cwd=ROOT / COMPONENTS[name],
            env=environment,
            check=False,
        )
        if result.returncode:
            failed.append(name)
    print("\nFailed: " + ", ".join(failed) if failed else "\nAll component suites passed.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
