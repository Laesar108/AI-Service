"""Blindly re-score preserved answers without changing or re-running the agents."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.score_responses import JUDGE
from eval.run_evaluation import SECRETS, safe

RUBRIC = JUDGE + """
추가 공통 규칙:
- applicability가 false인 conditions_pass/actionability_pass는 반드시 null이다.
- reference_runbooks는 공통 조치 기준이며 실제 조사한 증거가 아니다. 관측 사실은 tool_results와 history로만 검증한다.
- 평가 기준은 현재 질문/턴에 적용한다. 이름 기억은 이름을 다시 묻는 턴에 검사한다.
- request_pass는 질문별 criteria의 대상/비교/명시적 가정 요구도 확인한다.
- 조건을 올바르게 언급했어도 같은 답변에서 그 조건이 이미 확인됐다고 거짓 단정하면 실패다.
- 특정 조치를 권하지 않았다면 해당 조치의 조건 누락만으로 실패시키지 않는다.
- issues에는 실제 답변의 위반 구절과 이유를 적는다. 누락이면 누락이라고 명시한다.
- reasons 객체도 반환한다. grounding_pass, conditions_pass, request_pass, actionability_pass,
  irrelevant_advice 각각에 대해 답변의 문장을 근거로 한 간단한 판정 이유를 한 문장씩 적는다.
"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def key(row):
    return row['case_id'], row['repeat'], row['turn']


def main(args):
    # Source artifacts are read-only; only the new output directory receives scores.
    sources = {v: ROOT / 'eval/results' / name for v, name in
               [('before', 'before-live'), ('current', 'current')]}
    rows = {v: {key(r): r for r in read(p / 'responses.jsonl')} for v, p in sources.items()}
    common = sorted(rows['before'].keys() & rows['current'].keys())
    assert len(common) == 33
    assert all(rows['before'][k]['query'] == rows['current'][k]['query'] for k in common)
    cases = {c['id']: c for c in json.loads((ROOT / 'eval/fixtures/v1/cases.json').read_text())['cases']}
    protected = {str(p.relative_to(ROOT)): digest(p) for d in sources.values()
                 for p in d.rglob('*') if p.is_file()}
    protected.update({str(p.relative_to(ROOT)): digest(p)
                      for p in (ROOT / 'eval/baseline').rglob('*') if p.is_file()})
    jobs = [(v, k) for k in common for v in sources]
    random.Random(20261021).shuffle(jobs)
    out = ROOT / args.output
    out.mkdir(parents=True, exist_ok=args.resume)
    logging.disable(logging.CRITICAL)
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    SECRETS.extend(v for k, v in os.environ.items() if len(v) > 7 and
                   any(t in k.upper() for t in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')))
    from agent.openai_llm import OpenAIQualityLLM
    from agent.telemetry import price, RATES, PRICING_DATE
    from langsmith import Client, traceable, tracing_context
    model = os.getenv('OPENAI_QUALITY_MODEL', 'gpt-4o')
    client = Client(hide_inputs=safe, hide_outputs=safe)
    metadata = {'started_at': datetime.now(timezone.utc).isoformat(), 'model': model,
                'temperature': 0, 'records_per_variant': len(common), 'seed': 20261021,
                'rubric_sha256': hashlib.sha256(RUBRIC.encode()).hexdigest(),
                'source_hashes': protected, 'pricing': {'date': PRICING_DATE, 'rates': RATES},
                'scope': 'Preserved answers only; no agent reruns; one blind judgment per answer.',
                'order': [{'blind_id': f'answer-{i:03}', 'variant': v, 'key': k}
                          for i, (v, k) in enumerate(jobs)],
                'applicability': 'conditions/actionability: q01-q07 only; other fields: all 33',
                'tracing': 'Existing configured LangSmith setting retained; secrets redacted.'}
    if args.resume:
        previous = json.loads((out / 'metadata.json').read_text())
        for field in ('model', 'rubric_sha256', 'source_hashes', 'order'):
            assert json.loads(json.dumps(metadata[field])) == previous[field], field
    else:
        (out / 'metadata.json').write_text(json.dumps(safe(metadata), ensure_ascii=False, indent=2))
        (out / 'rubric.txt').write_text(RUBRIC)
    scores = out / 'scores.jsonl'
    done = {r['blind_id'] for r in read(scores)} if scores.exists() else set()

    def evaluate(i, variant, k):
        row = rows[variant][k]
        case = cases[row['case_id']]
        applicable = row['case_id'] in {f'q{x:02}' for x in range(1, 8)}
        payload = {'question': row['query'], 'turn': row['turn'],
                   'history': row['state'].get('history', []),
                   'criteria': case['expected']['checks'],
                   'applicability': {'conditions_pass': applicable, 'actionability_pass': applicable},
                   'reference_runbooks': {t: (ROOT / 'eval/fixtures/v1/runbooks' / f'{t}.md').read_text()
                                         for t in case['expected']['runbook_topics']},
                   'tool_results': row['tool_calls'], 'answer': row['final']}
        @traceable(name='saved_answer_blind_judge', process_inputs=safe, process_outputs=safe)
        def judge(inputs):
            llm = OpenAIQualityLLM(model, .005)
            text, usage = llm._chat(RUBRIC, json.dumps(inputs, ensure_ascii=False), json_mode=True)
            return {'judge': json.loads(text), 'usage': usage}
        start = time.perf_counter()
        with tracing_context(client=client):
            result = judge(safe(payload))
        # Preserve even malformed judgments; retries must never silently discard evidence.
        raw_dir = out / 'raw_judgments'
        raw_dir.mkdir(exist_ok=True)
        stamp = time.time_ns()
        (raw_dir / f'answer-{i:03}-{stamp}.json').write_text(
            json.dumps(safe(result), ensure_ascii=False, indent=2))
        score, usage = result['judge'], result['usage']
        for f in ('grounding_pass', 'request_pass', 'irrelevant_advice'):
            assert type(score.get(f)) is bool, 'invalid judgment'
        for f in ('conditions_pass', 'actionability_pass'):
            assert (type(score.get(f)) is bool) if applicable else (score.get(f) is None), 'invalid applicability'
        # Missing explanatory text does not invalidate otherwise well-typed scores.
        # Keep the model response unchanged and flag this omission for audit.
        actual = {c['arguments']['system_name'] for c in row['tool_calls']
                  if c.get('tool') == 'get_monitoring_status'}
        policy = case['expected']['target_policy']
        target_pass = (not actual) if policy in ('unknown', 'chat') else actual == set(case['expected']['targets'])
        return safe({'blind_id': f'answer-{i:03}', 'variant': variant, 'case_id': k[0],
                     'repeat': k[1], 'turn': k[2], 'judge': score, 'target_pass': target_pass,
                     'usage': usage, 'judge_estimated_usd': price(model, usage),
                     'explanation_missing': not isinstance(score.get('reasons'), dict),
                     'seconds': round(time.perf_counter() - start, 4)})

    failures = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(evaluate, i, v, k): i for i, (v, k) in enumerate(jobs)
                   if f'answer-{i:03}' not in done}
        for f in as_completed(futures):
            try:
                record = f.result()
                with scores.open('a') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                done.add(record['blind_id'])
                print(f'Completed {len(done)}/66', flush=True)
            except Exception as exc:
                failures.append({'blind_id': f'answer-{futures[f]:03}', 'error_type': type(exc).__name__})
                print('Judgment failed: ' + type(exc).__name__, flush=True)
    assert all(digest(ROOT / p) == h for p, h in protected.items()), 'source changed'
    completion = {'completed': len(done), 'expected': 66, 'source_hashes_unchanged': True,
                  'failures': failures, 'finished_at': datetime.now(timezone.utc).isoformat()}
    (out / 'completion.json').write_text(json.dumps(completion, indent=2))
    print(json.dumps(completion), flush=True)
    if failures:
        raise RuntimeError('Some judgments failed; resume required')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--resume', action='store_true')
    try:
        main(parser.parse_args())
    except Exception as exc:
        print('Rejudging stopped: ' + type(exc).__name__ + '; sensitive details suppressed.', flush=True)
        sys.exit(1)
