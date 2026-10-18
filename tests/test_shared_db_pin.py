"""SEC-system-hardening Step 5 follow-up 4: one shared_db pin per service.

A service names the `myblog_shared_db` commit it runs against in more than one
place: `requirements.txt` (the hand-edited source), `requirements.lock` (the
bundle input compiled from it), and any workflow step that checks out the
shared_db repository — today the canonical schema that CI loads into its
throwaway Postgres. Those copies must agree. When they did not, CI loaded a
schema without the tables the code read, and the failure read as a missing
migration rather than a stale workflow (worker's UndefinedTable class).

Different services pinning different shared_db commits is allowed and is not
checked here: skew *between* services is an intentional rollout state. Skew
*inside* one service never is.

The scan is deliberately strict. Every line of a tracked, non-Markdown file
that names the `hyuntohoon/myblog_shared_db` repository must be a form this
file recognises — a pip direct-URL pin, or a workflow checkout whose `ref:` is
either the literal pin or the value a step derives from `requirements.lock`.
A new kind of reference fails until it is taught here, rather than becoming a
copy nobody checks. Markdown is excluded so prose and links stay free.

This file is byte-identical in backend, music and worker; change all three.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()

SLUG = "hyuntohoon/myblog_shared_db"
PIP_PIN = re.compile(
    r"^myblog-shared-db @ git\+https://github\.com/hyuntohoon/myblog_shared_db\.git@([0-9a-f]{40})$",
    re.MULTILINE,
)
PIP_REF = re.compile(r"myblog_shared_db\.git@([^\s#;]+)")
CHECKOUT = re.compile(r"^(\s*)repository:\s*['\"]?hyuntohoon/myblog_shared_db['\"]?\s*(#.*)?$")
# The one non-literal ref accepted: a step output computed from the lock, so it
# cannot drift from it. See _derives_from_lock for what that step must contain.
DERIVED_REF = "${{ steps.shared_db_pin.outputs.ref }}"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _value(line: str) -> str:
    value = line.split(":", 1)[1]
    if not value.lstrip().startswith("${{"):
        value = value.split("#", 1)[0]
    return value.strip().strip("'\"")


def _sibling_ref(lines: list[str], idx: int) -> str | None:
    """The `ref:` in the same YAML mapping as the `repository:` at `lines[idx]`.

    Walks both directions while lines stay at the key's indent or deeper, so a
    `ref:` belonging to a neighbouring step is never picked up.
    """
    indent = _indent(lines[idx])
    for step in (-1, 1):
        j = idx + step
        while 0 <= j < len(lines):
            line = lines[j]
            if line.strip() and not line.lstrip().startswith("#"):
                if _indent(line) < indent:
                    break
                stripped = line.lstrip()
                if _indent(line) == indent and (stripped.startswith("- ") or stripped == "-"):
                    break
                if _indent(line) == indent and stripped.startswith("ref:"):
                    return _value(line)
            j += step
    return None


def _derivation_span(lines: list[str]) -> range:
    """Lines of the `shared_db_pin` step, if that step reads its ref out of requirements.lock.

    Those lines name the shared_db repository too (the pattern that extracts
    the pin), but they are the lock's reader, not a copy of the pin. An empty
    range means no such step, and then DERIVED_REF is not accepted either.
    """
    for idx, line in enumerate(lines):
        if re.fullmatch(r"\s*id:\s*shared_db_pin\s*", line):
            end = idx + 1
            while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) >= _indent(line)):
                end += 1
            # The extraction itself must read the lock: a mention elsewhere in
            # the step (an error message, a comment) proves nothing.
            if any("myblog_shared_db" in l and "requirements.lock" in l for l in lines[idx:end]):
                return range(idx, end)
    return range(0)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode()
    paths = []
    for name in out.split("\0"):
        path = ROOT / name
        if not name or path.suffix.lower() == ".md" or path.resolve() == THIS_FILE:
            continue
        if path.is_file():
            paths.append(path)
    return paths


def _references() -> tuple[list[tuple[str, str]], list[str]]:
    """(location, ref) for every recognised reference, and every unrecognised line."""
    refs: list[tuple[str, str]] = []
    unknown: list[str] = []
    for path in _tracked_files():
        data = path.read_bytes()
        if b"\0" in data or SLUG.encode() not in data:
            continue
        lines = data.decode("utf-8").splitlines()
        rel = path.relative_to(ROOT)
        in_workflow = rel.parts[0] == ".github"
        derivation = _derivation_span(lines) if in_workflow else range(0)
        for idx, line in enumerate(lines):
            if SLUG not in line or idx in derivation:
                continue
            where = f"{rel}:{idx + 1}"
            pip = PIP_REF.search(line)
            if pip:
                refs.append((where, pip.group(1)))
            elif CHECKOUT.match(line) and in_workflow:
                ref = _sibling_ref(lines, idx)
                if ref == DERIVED_REF and derivation:
                    continue
                refs.append((where, ref or "<no ref: checks out shared_db's default branch>"))
            else:
                unknown.append(f"{where}: {line.strip()}")
    return refs, unknown


def _pin(name: str) -> list[str]:
    return PIP_PIN.findall((ROOT / name).read_text(encoding="utf-8"))


def test_requirements_txt_has_exactly_one_commit_pin() -> None:
    pins = _pin("requirements.txt")
    assert len(pins) == 1, f"requirements.txt must pin myblog-shared-db to one 40-hex commit, found {pins}"


def test_requirements_lock_carries_the_same_pin() -> None:
    assert _pin("requirements.lock") == _pin("requirements.txt")


def test_every_shared_db_reference_uses_the_one_pin() -> None:
    (pin,) = _pin("requirements.txt")
    refs, unknown = _references()
    # Guard the guard: the scan must at least see requirements.txt and the lock.
    assert {where.split(":")[0] for where, _ in refs} >= {"requirements.txt", "requirements.lock"}
    assert not unknown, (
        "unrecognised reference to the shared_db repository — pin it to requirements.txt's "
        "commit and teach tests/test_shared_db_pin.py its shape:\n" + "\n".join(unknown)
    )
    drift = [f"{where} -> {ref}" for where, ref in refs if ref != pin]
    assert not drift, (
        f"shared_db pin drift inside this service; requirements.txt pins {pin}, but:\n"
        + "\n".join(drift)
        + "\nMove every copy in the same change (or derive the ref from requirements.lock)."
    )


# --- guard the guard: the YAML walk is the part easy to get subtly wrong ---

_WORKFLOW = """\
    steps:
      - uses: actions/checkout@v4
        with:
          ref: aaaa
      - uses: actions/checkout@v4
        with:
          repository: hyuntohoon/myblog_shared_db
          path: shared-db-schema
      - uses: actions/checkout@v4
        with:
          ref: bbbb  # trailing comment
          repository: hyuntohoon/myblog_shared_db
          token: x
"""


def test_sibling_ref_stays_inside_its_own_step() -> None:
    lines = _WORKFLOW.splitlines()
    first, second = [i for i, line in enumerate(lines) if CHECKOUT.match(line)]
    assert _sibling_ref(lines, first) is None  # the `ref: aaaa` above belongs to another step
    assert _sibling_ref(lines, second) == "bbbb"


def test_derived_ref_requires_a_step_that_reads_the_lock() -> None:
    reads_lock = """\
      - name: Resolve
        id: shared_db_pin
        run: |
          ref="$(sed -nE 's|myblog_shared_db.git@(.*)|\\1|p' requirements.lock)"
          test -n "$ref" || echo "no pin in requirements.lock"
      - uses: actions/checkout@v4
"""
    assert _derivation_span(reads_lock.splitlines()) == range(1, 5)  # stops before the next step
    reads_other = reads_lock.replace("|p' requirements.lock", "|p' pins.txt")
    assert not _derivation_span(reads_other.splitlines())  # the error message alone does not count
    assert not _derivation_span(reads_lock.replace("id: shared_db_pin", "id: other").splitlines())
    assert _value("          ref: ${{ steps.shared_db_pin.outputs.ref }}") == DERIVED_REF
