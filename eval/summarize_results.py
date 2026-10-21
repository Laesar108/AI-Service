"""Summarize measured usage, quality and overlap without rerunning the models."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]
BASE_CASES = {f"q{i:02}" for i in range(1, 11)}


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)] if values else None


# [계측 집계] 저장된 응답/채점만 집계한다. 기본 질문과 추가 경계 사례의 분모를 구분한다.
# API를 다시 호출하지 않으며 평가자 비용도 서비스 처리 비용과 분리한다.
def summarize(rows, scores):
    key = lambda r: (r["case_id"], r["repeat"], r["turn"])
    keys = {key(r) for r in rows}
    scores = [r for r in scores if key(r) in keys]
    incident = [r for r in rows if r["state"].get("route") == "incident" and any(c.get("tool") == "get_monitoring_status" and c.get("result", {}).get("status") != "unknown" for c in r["tool_calls"])]
    calls = [c for r in rows for c in r["llm_calls"]]
    specialists = [c for c in calls if c["stage"].startswith("researcher.")]
    bystage = defaultdict(list)
    for c in calls:
        bystage[c["stage"]].append(c)
    quality = {}
    incident_quality = {}
    incident_keys = {key(r) for r in incident}
    for field in ("grounding_pass", "conditions_pass", "request_pass", "actionability_pass"):
        vals = [r["judge"][field] for r in scores if r["judge"].get(field) is not None]
        quality[field] = {"passed": sum(v is True for v in vals), "total": len(vals)}
        vals = [r["judge"][field] for r in scores if key(r) in incident_keys and r["judge"].get(field) is not None]
        incident_quality[field] = {"passed": sum(v is True for v in vals), "total": len(vals)}
    overlaps = []
    for r in rows:
        retrieval = [c for c in r["llm_calls"] if c["stage"] == "retrieval"]
        workers = [c for c in r["llm_calls"] if c["stage"] in ("researcher.metrics", "researcher.logs")]
        if retrieval and workers:
            overlap = max(max(0, min(a["finished_at"], b["finished_at"]) -
                          max(a["started_at"], b["started_at"])) for a in retrieval for b in workers)
            overlaps.append(overlap)
    return {
        "messages": len(rows), "ok": sum(r["status"] == "ok" for r in rows), "scored": len(scores),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "chat_tokens": sum(r["chat_tokens"] for r in rows),
        "embedding_tokens": sum(r["embedding_tokens"] for r in rows),
        "estimated_usd": sum(r["estimated_usd"] for r in rows) if all(r["estimated_usd"] is not None for r in rows) else None,
        "median_seconds": statistics.median(r["seconds"] for r in rows) if rows else None,
        "p95_seconds": percentile([r["seconds"] for r in rows], .95),
        "incident_median_seconds": statistics.median(r["seconds"] for r in incident) if incident else None,
        "rewrites": sum(max(0, r["nodes"].count("write")-1) for r in rows),
        "tool_retries": sum(r["state"].get("retry_count", 0) for r in rows),
        "validation": dict(Counter(r["state"].get("validation_status", "not_applicable") for r in rows)),
        "specialist_calls": len(specialists),
        "specialist_output_tokens": sum(c["usage"]["output_tokens"] for c in specialists),
        "specialist_mean_output_tokens": statistics.mean(c["usage"]["output_tokens"] for c in specialists) if specialists else 0,
        "specialist_statuses": dict(Counter(f["status"] for r in rows for f in r["state"].get("findings", []))),
        "stage_usage": {stage: {"calls": len(cs), "tokens": sum(c["usage"]["tokens"] for c in cs),
                                "median_seconds": statistics.median(c["seconds"] for c in cs)}
                        for stage, cs in bystage.items()},
        "retrieval_worker_overlap": {"eligible_requests": len(overlaps),
            "overlapping_requests": sum(s > 0 for s in overlaps),
            "median_overlap_seconds": statistics.median(overlaps) if overlaps else None},
        "quality": quality, "incident_quality": incident_quality,
        "irrelevant_advice": sum(r["judge"]["irrelevant_advice"] for r in scores),
        "target_pass": {"passed": sum(r["target_pass"] for r in scores), "total": len(scores)},
        "judge_estimated_usd": sum(r["judge_estimated_usd"] or 0 for r in scores),
    }


def main(args):
    root = ROOT / args.input
    variants = json.loads((root / "metadata.json").read_text())["variants"]
    all_rows = {v: read(root / v / "responses.jsonl") for v in variants}
    all_scores = {v: read(root / v / "scores.jsonl") if (root/v/"scores.jsonl").exists() else [] for v in variants}
    groups = {"all": lambda r: True, "main": lambda r: r["case_id"] in BASE_CASES,
              "boundaries": lambda r: r["case_id"] not in BASE_CASES,
              "simple_main": lambda r: r["case_id"] in ("q01", "q02", "q03", "q07"),
              "complex_main": lambda r: r["case_id"] in ("q04", "q05", "q06")}
    summary = {g: {v: summarize([r for r in all_rows[v] if pred(r)], all_scores[v]) for v in variants}
               for g, pred in groups.items()}
    (root/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    transcript = ["# 최초 원본과 현재 버전 응답 비교", "", "각 구성의 첫 번째 반복. 전체 원문과 호출 기록은 responses.jsonl에 보존.", ""]
    grouped = {v: {(r["case_id"], r["turn"]): r for r in rows if r["repeat"] == 1} for v, rows in all_rows.items()}
    for key, row in grouped[variants[0]].items():
        transcript += [f"## {key[0]} / turn {key[1]}", "", row["query"], ""]
        for v in variants:
            transcript += [f"### {v}", "", grouped[v][key]["final"], ""]
    (root/"responses_readable.md").write_text("\n".join(transcript))
    selection = [{"case_id": r["case_id"], "repeat": r["repeat"],
                  "selection": r["state"].get("research_selection", {}),
                  "findings": r["state"].get("findings", [])} for r in all_rows.get("after", [])]
    (root/"selection_records.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2))
    print(json.dumps({g: {v: {k: d[k] for k in ("messages", "total_tokens", "estimated_usd", "median_seconds", "specialist_calls", "quality")}
                         for v, d in configs.items()} for g, configs in summary.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--input", required=True)
    main(p.parse_args())
