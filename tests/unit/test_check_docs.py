from pathlib import Path

from scripts.check_docs import check_repository


def _write(path: Path, content: str = "# Document\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_check_repository_accepts_valid_relative_links(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "[Guide](docs/guide.md#start)\n")
    _write(tmp_path / "docs/guide.md")

    errors = check_repository(
        tmp_path,
        active_documents=("README.md", "docs/guide.md"),
        require_core_index=False,
    )

    assert errors == []


def test_check_repository_reports_source_line_for_broken_link(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# Project\n\n[Missing](docs/missing.md)\n")

    errors = check_repository(
        tmp_path,
        active_documents=("README.md",),
        require_core_index=False,
    )

    assert errors == ["README.md:3: broken link: docs/missing.md"]


def test_check_repository_ignores_links_in_code_fences_and_external_urls(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "README.md",
        """# Project

[Website](https://example.com/docs)

~~~markdown
[Example](missing.md)
~~~
""",
    )

    errors = check_repository(
        tmp_path,
        active_documents=("README.md",),
        require_core_index=False,
    )

    assert errors == []


def test_check_repository_rejects_links_outside_repository(tmp_path: Path) -> None:
    _write(tmp_path / "docs/guide.md", "[Outside](../../outside.md)\n")

    errors = check_repository(
        tmp_path,
        active_documents=("docs/guide.md",),
        require_core_index=False,
    )

    assert errors == ["docs/guide.md:1: link escapes repository: ../../outside.md"]


def test_check_repository_rejects_noncanonical_docs_artifacts_when_enabled(tmp_path: Path) -> None:
    _write(tmp_path / "docs/README.md")
    _write(tmp_path / "docs/old-implementation-prompt.md")
    (tmp_path / "docs/stale-screenshot.png").write_bytes(b"stale")

    errors = check_repository(
        tmp_path,
        active_documents=("docs/README.md",),
        require_core_index=False,
        enforce_canonical_docs=True,
    )

    assert errors == [
        "docs/old-implementation-prompt.md: non-canonical docs artifact; merge current facts "
        "into the documentation index or move generated output outside the repository",
        "docs/stale-screenshot.png: non-canonical docs artifact; merge current facts "
        "into the documentation index or move generated output outside the repository",
    ]
