"""Validate links and reject extra files in the maintained documentation set."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ACTIVE_DOCUMENTS = (
    "README.md",
    "docs/architecture.md",
    "docs/development.md",
    "docs/api-contract.md",
    "docs/data-model.md",
)

_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:")


def _links_in_file(path: Path) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    fence_marker: str | None = None

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if fence_marker is None:
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue

        for match in _LINK_RE.finditer(line):
            destination = match.group(1).strip("<>")
            links.append((line_number, destination))

    return links


def _resolve_local_link(source: Path, destination: str) -> Path | None:
    lowered = destination.lower()
    if not destination or destination.startswith(("#", "/")):
        return None
    if lowered.startswith(_EXTERNAL_SCHEMES) or "://" in destination:
        return None

    file_part = unquote(destination.split("#", 1)[0].split("?", 1)[0])
    if not file_part:
        return None
    return (source.parent / file_part).resolve()


def check_repository(
    root: Path,
    *,
    active_documents: tuple[str, ...] = ACTIVE_DOCUMENTS,
    enforce_canonical_docs: bool = False,
) -> list[str]:
    """Return human-readable validation errors for ``root``."""
    root = root.resolve()
    errors: list[str] = []
    for relative_path in active_documents:
        source = root / relative_path
        if not source.is_file():
            errors.append(f"{relative_path}: required active document is missing")
            continue

        for line_number, destination in _links_in_file(source):
            target = _resolve_local_link(source, destination)
            if target is None:
                continue
            try:
                target.relative_to(root)
            except ValueError:
                errors.append(
                    f"{relative_path}:{line_number}: link escapes repository: {destination}"
                )
                continue
            if not target.exists():
                errors.append(f"{relative_path}:{line_number}: broken link: {destination}")

    if enforce_canonical_docs:
        canonical = {
            (root / relative_path).resolve()
            for relative_path in active_documents
            if relative_path.startswith("docs/")
        }
        docs_root = root / "docs"
        for path in sorted(item for item in docs_root.rglob("*") if item.is_file()) if docs_root.is_dir() else ():
            if path.resolve() not in canonical:
                errors.append(
                    f"{path.relative_to(root)}: non-canonical docs artifact; merge current facts "
                    "into the documentation index or move generated output outside the repository"
                )

    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check_repository(root, enforce_canonical_docs=True)
    if errors:
        print("Documentation check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Documentation check passed ({len(ACTIVE_DOCUMENTS)} active documents).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
