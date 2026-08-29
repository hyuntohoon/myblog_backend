#!/usr/bin/env python3
"""Print a stable name/version manifest for a pip --target directory."""

from __future__ import annotations

import re
import sys
from importlib.metadata import distributions
from pathlib import Path


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


if len(sys.argv) != 2:
    raise SystemExit("usage: distribution_manifest.py TARGET_DIR")

target = Path(sys.argv[1])
records = sorted(
    f"{canonical_name(dist.metadata['Name'])}=={dist.version}"
    for dist in distributions(path=[str(target)])
)
print(*records, sep="\n")
