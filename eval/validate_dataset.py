"""앱을 불러오거나 환경 설정 파일을 읽지 않고 고정 평가 입력을 오프라인으로 검사한다."""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


# [평가 데이터 검증] 데이터 해시와 사례 구조를 검사해 Before/After 입력이 바뀌는 것을 방지한다.
def validate(root):
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    # 목록에 적힌 임의 경로를 따르지 않고 명시적으로 허용한 고정 평가 파일만 읽는다.
    expected_files = {
        "cases.json",
        *("runbooks/" + name + ".md" for name in (
            "cpu-spike", "memory-leak", "disk-full", "high-latency",
            "network-timeout", "db-down",
        )),
    }
    require(set(manifest["sha256"]) == expected_files, "Manifest file list mismatch")
    for name in sorted(expected_files):
        path = root / name
        require(not path.is_symlink(), f"Symlinks are not allowed: {name}")
        require(path.resolve().is_relative_to(root.resolve()), f"Outside fixture: {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        require(digest == manifest["sha256"][name], f"Fixture changed: {name}")

    data = read_json(root / "cases.json")
    require(data["schema_version"] == 1, "Unsupported schema version")
    require(data["data_kind"] == "synthetic", "Synthetic data label required")
    require(data["dataset_id"] == manifest["dataset_id"], "Dataset ID mismatch")
    known = set(data["known_systems"])
    require(len(known) == len(data["known_systems"]), "Duplicate system")
    require(known == set(data["monitoring"]), "System registry mismatch")
    for name, value in data["monitoring"].items():
        require(name == value["system_name"], "Metric system mismatch")
        require(value["status"] in ("healthy", "degraded", "down"), "Unknown status")
        for metric in ("cpu_percent", "memory_percent"):
            require(type(value[metric]) in (int, float) and 0 <= value[metric] <= 100,
                    f"Invalid percentage: {name}/{metric}")
        require(datetime.fromisoformat(value["last_checked"]).tzinfo is not None,
                "Timezone required")
        require(isinstance(value["alerts"], list) and
                all(isinstance(a, str) for a in value["alerts"]), "Invalid alerts")
    ids = [case["id"] for case in data["cases"]]
    require(len(ids) == len(set(ids)), "Duplicate case ID")
    require({f"q{i:02}" for i in range(1, 11)} <= set(ids), "Missing base question")
    topics = {Path(name).stem for name in expected_files if name.startswith("runbooks/")}
    for case in data["cases"]:
        label = case["id"]
        require(case["session_policy"] == "fresh_per_case_shared_between_turns",
                f"Invalid session policy: {label}")
        require(case["turns"] and all(isinstance(t, str) and t.strip()
                                    for t in case["turns"]), f"Empty query: {label}")
        require(len(case["turns"]) == (2 if case["question_number"] == 10 else 1),
                f"Turn count mismatch: {label}")
        expected = case["expected"]
        require(expected["target_policy"] in ("exact", "assume_or_clarify", "unknown", "chat"),
                f"Unknown target policy: {label}")
        require(set(expected["targets"]) <= known, f"Unknown expected target: {label}")
        require(set(expected["runbook_topics"]) <= topics, f"Missing runbook: {label}")
        require(expected["checks"] and all(c["id"] and c["criterion"]
                                          for c in expected["checks"]),
                f"Missing evaluation criteria: {label}")
        check_ids = [c["id"] for c in expected["checks"]]
        require(len(check_ids) == len(set(check_ids)), f"Duplicate criterion: {label}")
        for name, overrides in case["monitoring_overrides"].items():
            require(name in known, f"Unknown override target: {label}")
            require(set(overrides) <= {"cpu_percent", "memory_percent"},
                    f"Unexpected override field: {label}")
            require(all(type(v) in (int, float) and 0 <= v <= 100
                        for v in overrides.values()), f"Invalid override value: {label}")
        require(set(case["additional_evidence"]) == {"logs", "cpu_series", "deployments"},
                f"Missing evidence availability: {label}")
        for kind, evidence in case["additional_evidence"].items():
            require(evidence["availability"] in ("provided", "not_provided"),
                    f"Invalid evidence availability: {label}")
            require(isinstance(evidence["records"], list), "Evidence records must be a list")
            require(evidence["availability"] == "provided" or not evidence["records"],
                    f"Unavailable evidence has records: {label}/{kind}")
            for record in evidence["records"]:
                require(record["system_name"] in known, f"Unknown evidence target: {label}")
                require(datetime.fromisoformat(record["observed_at"]).tzinfo is not None,
                        f"Evidence timezone missing: {label}")
                if kind == "cpu_series":
                    require(type(record["cpu_percent"]) in (int, float)
                            and 0 <= record["cpu_percent"] <= 100, "Invalid CPU sample")
        samples = case["additional_evidence"]["cpu_series"]["records"]
        times = [datetime.fromisoformat(r["observed_at"]) for r in samples]
        require(times == sorted(set(times)), f"Unordered or duplicate samples: {label}")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path,
                        default=Path(__file__).parent / "fixtures" / "v1")
    args = parser.parse_args()
    try:
        data = validate(args.dataset)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"FAIL: {exc}\n")
    print(f"PASS: {data['dataset_id']} — {len(data['cases'])} cases, "
          f"{len(data['monitoring'])} systems, 6 frozen runbooks")
    print("Offline fixture validation only; no LLM responses were evaluated.")


if __name__ == "__main__":
    main()
