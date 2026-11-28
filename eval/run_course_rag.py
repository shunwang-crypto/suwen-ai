"""Exercise an imported D2L course through real retrieval, source-context and chat HTTP APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx

GOLDEN = Path(__file__).parent / "golden_set/deep-learning-course.jsonl"


async def run(args):
    receipt = json.loads(args.receipt.expanduser().read_text())
    cases = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line]
    api_root = "/api/kb/" + receipt["course_id"]
    mapping = {item["id"]: item["pages"][0].split("/")[0] for item in receipt["documents"]}
    report = {
        "course_id": receipt["course_id"],
        "source_revision": receipt["revision"],
        "cases": [],
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temp = args.output.with_suffix(".tmp")
        temp.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        temp.replace(args.output)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=240) as api:
        token = os.getenv("EDUAGENTS_TOKEN")
        if not token and args.demo:
            from scripts.seed_demo import DEMO_EMAIL, DEMO_PASSWORD

            login = await api.post(
                "/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
            )
            login.raise_for_status()
            token = login.json()["access_token"]
        if not token:
            raise ValueError("set EDUAGENTS_TOKEN or use --demo")
        api.headers["Authorization"] = "Bearer " + token

        async def get(path):
            response = await api.get(path)
            response.raise_for_status()
            return response.json()

        start = time.monotonic()
        while True:
            docs = await get(api_root + "/documents")
            if any(d["status"] == "failed" for d in docs):
                raise RuntimeError(
                    [(d["filename"], d["error_msg"]) for d in docs if d["status"] == "failed"]
                )
            if {d["id"] for d in docs} == set(mapping) and all(
                d["status"] in {"ready", "enriched"} for d in docs
            ):
                break
            if time.monotonic() - start > 1800:
                raise TimeoutError("course ingestion")
            await asyncio.sleep(3)
        chunks = {}
        children = embedded = tagged = 0
        for doc in docs:
            offset = 0
            while True:
                batch = await get(
                    api_root + f"/documents/{doc['id']}/chunks?offset={offset}&limit=200"
                )
                for chunk in batch:
                    if chunk["id"] in chunks:
                        raise ValueError("duplicate chunk in paginated course index")
                    chunks[chunk["id"]] = doc["id"]
                    if chunk["parent_id"]:
                        children += 1
                        embedded += chunk["embedding_status"] == "done"
                        tagged += bool(chunk["kc_ids"])
                if len(batch) < 200:
                    break
                offset += len(batch)
        report.update(
            documents=len(docs),
            parents=len(chunks) - children,
            children=children,
            embedded=embedded,
            children_with_section=tagged,
        )
        if (
            not children
            or embedded != children
            or tagged != children
        ):
            raise ValueError("incomplete course index")
        save()

        def relevant(case, doc_id, heading):
            return mapping.get(doc_id) == case.get("chapter") and case["section"] in heading

        for case in cases:
            row = {"id": case["id"], "question": case["q"], "answerable": "chapter" in case}
            search = await api.post(api_root + "/debug/retrieve", json={"query": case["q"]})
            search.raise_for_status()
            result = search.json()
            hits = result["chunks"]
            if any(hit["chunk_id"] not in chunks for hit in hits):
                raise ValueError("retrieval returned a foreign or deleted chunk")
            row["dense_hits"], row["sparse_hits"] = result["dense_hits"], result["sparse_hits"]
            if row["answerable"]:
                row["rank"] = next(
                    (
                        i
                        for i, h in enumerate(hits, 1)
                        if relevant(case, chunks[h["chunk_id"]], h["heading_path"])
                    ),
                    None,
                )
            report["cases"].append(row)
            save()
        for case, row in zip(cases, report["cases"], strict=True):
            if not case.get("live"):
                continue
            began = time.monotonic()
            last_event = ""
            attempt = None
            answer, citations, done, error = "", [], None, None
            text = case["q"] + " 请仅依据本课程教材回答，若缺少依据请明确说明。"
            async with api.stream(
                "POST",
                "/api/chat",
                json={"mode": "fast", "kb_ids": [receipt["course_id"]], "message": text},
            ) as stream:
                stream.raise_for_status()
                async for line in stream.aiter_lines():
                    if line.startswith("event:"):
                        last_event = line[6:].strip()
                    if not line.startswith("data:"):
                        continue
                    data = json.loads(line[5:].strip())
                    if last_event == "token":
                        if attempt != data.get("attempt"):
                            answer = ""
                            attempt = data.get("attempt")
                        answer += data.get("text", "")
                    elif last_event == "citation":
                        citations.append(data)
                    elif last_event == "done":
                        done = data
                    elif last_event == "error":
                        error = data.get("code", "CHAT_ERROR")
            selected = (done or {}).get(
                "selected_citations",
                citations if (done or {}).get("usage", {}).get("mode") == "cache" else [],
            )
            resolved = []
            for cite in selected:
                cid = cite.get("chunk_id")
                if cid not in chunks:
                    raise ValueError("citation outside course")
                context = await get(api_root + f"/chunks/{cid}/context")
                if context["chunk"]["id"] != cid or context["doc_id"] != chunks[cid]:
                    raise ValueError("citation context mismatch")
                resolved.append(
                    {
                        "id": cid,
                        "doc_id": context["doc_id"],
                        "heading": context["chunk"]["heading_path"],
                    }
                )
            refused = bool((done or {}).get("refused"))
            reason = (done or {}).get("refusal_reason")
            verified = (
                any(relevant(case, c["doc_id"], c["heading"]) for c in resolved)
                if row["answerable"]
                else False
            )
            terms = all(any(term in answer for term in group) for group in case.get("terms", []))
            contradictions = [claim for claim in case.get("contradictions", []) if claim in answer]
            correct = (
                (not refused and verified and terms and not contradictions)
                if row["answerable"]
                else (
                    refused
                    and not resolved
                    and reason in {"insufficient_evidence", "no_evidence", "unsupported_answer"}
                )
            )
            row["chat"] = {
                "passed": bool(done and answer and not error and correct),
                "refused": refused,
                "error": error,
                "answer": answer,
                "refusal_reason": reason,
                "citations": resolved,
                "section_match": verified,
                "term_match": terms,
                "contradictions": contradictions,
                "session_id": (done or {}).get("session_id"),
                "cache": (done or {}).get("usage", {}).get("mode") == "cache",
                "wall_s": round(time.monotonic() - began, 1),
            }
            save()
            print(
                case["id"],
                "pass" if row["chat"]["passed"] else "fail",
                row["chat"]["wall_s"],
                flush=True,
            )
        ranks = [r["rank"] for r in report["cases"] if r["answerable"]]
        report["summary"] = {
            "answerable": len(ranks),
            "hit1": ranks.count(1),
            "hit3": sum(bool(rank and rank <= 3) for rank in ranks),
            "chat": dict(
                Counter(
                    "passed" if r["chat"]["passed"] else "failed"
                    for r in report["cases"]
                    if "chat" in r
                )
            ),
        }
        save()
        print(report["summary"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receipt",
        type=Path,
        default=Path("~/.local/share/eduagents/course-imports/d2l-zh/import.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--demo", action="store_true")
    asyncio.run(run(parser.parse_args()))
