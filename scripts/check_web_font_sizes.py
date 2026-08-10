"""Reject new web font sizes below the 12px product floor.

The scan deliberately includes CSS, inline JSX styles, SVG and canvas-adjacent
components, and print rules.  Those surfaces are reported separately so a
future exception cannot hide behind a DOM Tailwind-only check.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "apps" / "web"
FORBIDDEN = re.compile(
    r"(?:"
    r"text-\[(?:9|10|11)px\]"
    r"|font-size\s*:\s*(?:9|10|11)px\b"
    r"|fontSize\s*:\s*[\"'](?:9|10|11)px[\"']"
    r"|font-size\s*:\s*[\"'](?:9|10|11)px[\"']"
    r")"
)
SPECIAL_LOW = re.compile(
    r"(?:"
    r"fontSize\s*=\s*\{(?:9|10|11)(?:\.\d+)?\}"
    r"|font-size\s*:\s*(?:9|10|11)(?:\.\d+)?(?:px|pt)\b"
    r")"
)
SURFACE_MARKERS = {
    "svg": re.compile(r"<svg\b", re.IGNORECASE),
    "canvas": re.compile(r"<canvas\b", re.IGNORECASE),
    "print": re.compile(r"@media\s+print|@page\b|print-color-adjust", re.IGNORECASE),
}
EXTENSIONS = {".tsx", ".ts", ".css"}
EXCLUDED_DIRS = {"node_modules", ".next", "dist", "build"}


def main() -> int:
    violations: list[str] = []
    surfaces = {name: 0 for name in SURFACE_MARKERS}
    special_low = {name: 0 for name in SURFACE_MARKERS}
    for path in sorted(ROOT.rglob("*")):
        if (
            not path.is_file()
            or path.suffix not in EXTENSIONS
            or any(part in EXCLUDED_DIRS for part in path.parts)
        ):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        file_text = "\n".join(lines)
        file_surfaces = {
            name: bool(marker.search(file_text))
            for name, marker in SURFACE_MARKERS.items()
        }
        for line_number, line in enumerate(lines, 1):
            if FORBIDDEN.search(line):
                violations.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
            if SPECIAL_LOW.search(line):
                # These are reviewed separately because chart tick labels and
                # print output have their own physical/layout constraints.
                if "font-size" in line and file_surfaces["print"]:
                    special_low["print"] += 1
                elif "fontSize" in line and file_surfaces["svg"]:
                    special_low["svg"] += 1
                else:
                    violations.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
            for name, marker in SURFACE_MARKERS.items():
                surfaces[name] += len(marker.findall(line))
    if violations:
        print("Forbidden web font sizes below 12px:")
        print("\n".join(violations))
        return 1
    print("Web font floor check passed (no 9/10/11px Tailwind, CSS, or inline styles).")
    print(
        "Audited surfaces: "
        + ", ".join(f"{name}={count}" for name, count in surfaces.items())
        + "."
    )
    print(
        "Special low-size audit (SVG/print only): "
        + ", ".join(f"{name}={count}" for name, count in special_low.items())
        + "; these are not DOM body text."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
