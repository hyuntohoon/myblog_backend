#!/usr/bin/env python3
"""Print a stable manifest for a pip --target directory.

Two lines per distribution is not enough to claim a repeatable bundle: a
name==version manifest compares equal for two different wheels of the same
version, and for two different local builds of a source dependency. So the
manifest also carries a content digest over every installed file, which is
what actually decides whether two bundles are the same bundle.

Bytecode caches are excluded: they are pruned before the zip is built, and
they embed the compiling interpreter's paths and timestamps.
"""

from __future__ import annotations

import hashlib
import re
import sys
from importlib.metadata import distributions
from pathlib import Path


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def tree_digest(target: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in target.rglob("*") if p.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(str(path.relative_to(target)).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


if len(sys.argv) != 2:
    raise SystemExit("usage: distribution_manifest.py TARGET_DIR")

target = Path(sys.argv[1])
records = sorted(
    f"{canonical_name(dist.metadata['Name'])}=={dist.version}"
    for dist in distributions(path=[str(target)])
)
print(*records, sep="\n")
print(f"# tree-sha256 {tree_digest(target)}")
