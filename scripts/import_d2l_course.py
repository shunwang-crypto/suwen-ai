"""Import the Chinese D2L textbook from its pinned published TOC into a local course."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

REV = "e6b18ccea71451a55fcd861d7b96fddf2587b09a"
REPO = "https://github.com/d2l-ai/d2l-zh"
RAW = f"https://raw.githubusercontent.com/d2l-ai/d2l-zh/{REV}/"
TOC = re.compile(r"^`{3}toc\s*\n(.*?)^`{3}\s*$", re.M | re.S)


def children(markdown):
    return [
        line.strip()
        for block in TOC.findall(markdown)
        for line in block.splitlines()
        if re.fullmatch(r"(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+", line.strip())
    ]


def section(text, path, level):
    url = f"{REPO}/blob/{REV}/{path}"
    body = TOC.sub("", text).strip()
    # Rebase Markdown headings outside fenced code. Keep every framework variant.
    out, fenced = [], False
    for line in body.splitlines():
        if re.match(r"^\s*`{3,}", line):
            fenced = not fenced
        if not fenced:
            line = re.sub(r"^(#{1,6})(?= )", lambda m: "#" * min(6, len(m[1]) + level), line)
            line = re.sub(
                r"(!?\[[^\]]*\]\()((?:\.\.?/)[^)\s]+)(\))",
                lambda m: m[1] + urljoin(RAW + path, m[2]) + m[3],
                line,
            )
        out.append(line)
    return f"{out[0]}\n\n原文：{url}\n\n" + "\n".join(out[1:])


async def main(args):
    base = args.work_dir.expanduser().resolve()
    rawdir = base / REV / "sources"
    rawdir.mkdir(parents=True, exist_ok=True)
    slots = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as web:

        async def fetch(path):
            target = rawdir / path
            if not target.exists():
                async with slots:
                    for attempt in range(4):
                        try:
                            response = await web.get(RAW + path)
                            response.raise_for_status()
                            break
                        except (httpx.TransportError, httpx.HTTPStatusError):
                            if attempt == 3:
                                raise
                            await asyncio.sleep(2**attempt)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(response.content)
            return target.read_text("utf-8")

        root = await fetch("index.md")
        chapter_paths = [v + ".md" for v in children(root)]
        if not chapter_paths:
            raise ValueError("empty upstream TOC")

        async def build(path):
            first = await fetch(path)
            if path == "chapter_references/zreferences.md":
                bib = await fetch("d2l.bib")
                bibliography = re.sub(r"(?m)^(@[A-Za-z]+\{([^,\n]+),)", r"## \2\n\n\1", bib)
                content = f"# 参考文献\n\n原文：{REPO}/blob/{REV}/d2l.bib\n\n{bibliography}"
                return "参考文献", content.encode(), [path, "d2l.bib"]
            parts = [str(Path(path).parent / (child + ".md")) for child in children(first)]
            texts = await asyncio.gather(*(fetch(child) for child in parts))
            title = first.strip().splitlines()[0].strip("# ").strip()
            doc = (
                section(first, path, 0)
                + "\n\n"
                + "\n\n".join(section(t, p, 1) for p, t in zip(parts, texts, strict=True))
            )
            content = (doc.rstrip() + "\n").encode()
            return title, content, [path, *parts]

        chapters = await asyncio.gather(*(build(path) for path in chapter_paths))
    receipt = base / "import.json"
    if receipt.exists():
        report = json.loads(receipt.read_text())
        if report["revision"] != REV:
            raise ValueError("existing receipt belongs to another edition")
    else:
        report = {"revision": REV, "source": REPO, "course_id": None, "documents": []}

    def save():
        temp = receipt.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temp.replace(receipt)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=120) as api:
        if token := os.getenv("EDUAGENTS_TOKEN"):
            api.headers["Authorization"] = "Bearer " + token
        elif args.demo:
            from seed_demo import DEMO_EMAIL, DEMO_PASSWORD

            login = await api.post(
                "/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
            )
            login.raise_for_status()
            api.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        else:
            raise ValueError("set EDUAGENTS_TOKEN or select --demo for the local demo account")
        if not report["course_id"]:
            kb = await api.post(
                "/api/kb", json={"name": "深度学习 · 动手学深度学习", "subject": "深度学习"}
            )
            kb.raise_for_status()
            report["course_id"] = kb.json()["id"]
            save()
        route = f"/api/kb/{report['course_id']}/documents"
        listing = await api.get(route)
        listing.raise_for_status()
        known = {d["id"] for d in listing.json()}
        for idx, (title, data, paths) in enumerate(chapters, 1):
            filename = f"{idx:02d} {title}.md"
            digest = hashlib.sha256(data).hexdigest()
            previous = next((d for d in report["documents"] if d["pages"][0] == paths[0]), {})
            if previous.get("id") in known and previous.get("sha256") == digest:
                continue
            uploaded = await api.post(
                route,
                params={"enrichment": "headings"},
                files={"file": (filename, data, "text/markdown")},
            )
            uploaded.raise_for_status()
            if previous.get("id") in known and previous["id"] != uploaded.json()["id"]:
                removed = await api.delete(route + "/" + previous["id"])
                removed.raise_for_status()
            report["documents"] = [d for d in report["documents"] if d["pages"][0] != paths[0]]
            report["documents"].append(
                {
                    "id": uploaded.json()["id"],
                    "filename": filename,
                    "pages": paths,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
            save()
            print(f"{idx}/{len(chapters)} {filename}", flush=True)
        print(
            f"course={report['course_id']} sources={sum(len(d['pages']) for d in report['documents'])} receipt={receipt}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--work-dir", type=Path, default=Path("~/.local/share/eduagents/course-imports/d2l-zh")
    )
    asyncio.run(main(parser.parse_args()))
