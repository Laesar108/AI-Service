"""규칙 기반 정책: 대상, 근거 확보 여부, 수치와 조치 조건을 검사한다."""
import json
import re
from pathlib import Path

REGISTRY = Path(__file__).resolve().parents[1] / 'data/monitoring.json'


def known_systems():
    return set(json.loads(REGISTRY.read_text()))


# [대상 식별] 등록 목록과 이름 경계를 확인해 대상 누락·부분 문자열 오인식을 줄인다.
# 로그인→auth-service 추정은 남겨 두되, 추정 사실을 별도로 전달하는 부분 개선이다.
def identify_targets(query, known=None):
    known = known if known is not None else known_systems()
    # 이름 경계를 확인해 fake-auth-service의 일부를 auth-service로 인식하지 않도록 한다.
    names = re.findall(r'(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_-]*-service(?![A-Za-z0-9_-])|(?<![A-Za-z0-9_-])web-\d+(?![A-Za-z0-9_-])', query)
    explicit = list(dict.fromkeys(names))
    for name in sorted(known):
        if re.search(r'(?<![A-Za-z0-9_-])'+re.escape(name)+r'(?![A-Za-z0-9_-])', query) and name not in explicit:
            explicit.append(name)
    targets = [n for n in explicit if n in known]
    unknown = [n for n in explicit if n not in known]
    assumption = ''
    if not explicit and '로그인' in query and 'auth-service' in known:
        targets = ['auth-service']
        assumption = '로그인 기능을 auth-service로 가정해 조사했습니다. 다른 서비스라면 정확한 이름을 알려주세요.'
    return targets, unknown, assumption


def topics_for(metric):
    if metric.get('status') == 'unknown':
        return []
    text = ' '.join(metric.get('alerts', [])).lower()
    topics = []
    rules = [('high-latency', ('latency','pool')),('cpu-spike',('cpu',)),
             ('memory-leak',('memory','heap')),('network-timeout',('timeout','network','packet')),
             ('disk-full',('disk',))]
    for topic, terms in rules:
        if any(term in text for term in terms):
            topics.append(topic)
    if metric.get('cpu_percent',0) >= 90 and 'cpu-spike' not in topics:
        topics.append('cpu-spike')
    if metric.get('memory_percent',0) >= 90 and 'memory-leak' not in topics:
        topics.append('memory-leak')
    if metric.get('status') == 'down':
        topics.append('db-down')
    return list(dict.fromkeys(topics))


# [근거 누락 확인] 로그/시계열/배포 자료가 없으면 미확인으로 기록한다. 빈 자료를 정상 증거로 삼지 않는다.
def evidence_gaps(collected, targets):
    gaps = []
    for target in targets:
        metric = next((c for c in collected if c['source']=='monitoring' and c.get('target')==target),None)
        if not metric or metric.get('error') or metric['data'].get('status')=='unknown':
            gaps.append(f'{target}: 모니터링 자료 없음')
        diagnostic = next((c['data'] for c in collected if c['source']=='evidence' and c.get('target')==target),{})
        for kind, label in [('logs','로그'),('cpu_series','CPU 시계열'),('deployments','배포 이력')]:
            if diagnostic.get(kind,{}).get('availability')!='provided':
                gaps.append(f'{target}: {label} 미조회 또는 미제공')
    for c in collected:
        if c.get('error'):
            gaps.append(f"{c.get('target','')}: {c['source']} 조회 실패")
    return list(dict.fromkeys(gaps))


def observation_numbers(collected):
    values = set()
    for c in collected:
        if c['source'] in ('monitoring','evidence','runbook'):
            values.update(float(n) for n in re.findall(r'(?<![\w])\d+(?:\.\d+)?',json.dumps(c['data'],ensure_ascii=False)))
    return values


# [검증 개선] 근거에 없는 ID·백분율과 일부 명시적 잘못된 권고를 검사한다.
# 모든 인과/조건 오류를 검출하는 규칙은 아니므로 별도의 LLM 검토와 원문 점검이 필요하다.
def deterministic_errors(draft, collected):
    errors=[]
    references={c.get('id',f"{c['source']}:{c['target']}") for c in collected}
    for group in re.findall(r'\[(?:ID:\s*)?((?:monitoring|evidence|runbook):[^\]]+)\]',draft):
        # 한 괄호에서 여러 근거를 인용할 수 있으므로 각 근거 ID를 따로 검사한다.
        # [버그 수정] 한 괄호에 여러 ID를 인용한 정상 응답의 불필요한 재작성을 방지한다.
        for part in group.split(','):
            reference = re.sub(r'^ID:\s*', '', part.strip())
            if reference not in references:
                errors.append('조회 근거에 없는 참조 ID: '+reference)
    for sentence in re.split(r'\n|(?<=[다요])\.\s*',draft):
        if ('get_monitoring_status' in sentence
                and re.search(r'시계열|배포|로그|추세|지속\s*여부',sentence)
                and not re.search(r'불가|못|않|없|반환하지|제공하지',sentence)):
            errors.append('get_monitoring_status는 현재 모의 지표만 반환하므로 시계열·로그·배포 이력·실제 추세 조회 수단으로 안내할 수 없음: '+sentence.strip())
        if re.search(r'메모리.{0,50}(미만|낮).{0,60}(배제|문제.{0,8}아니)',sentence):
            if not re.search(r'배제.{0,40}(없|않|못|아니)',sentence):
                errors.append('낮은 현재 메모리 수치만으로 메모리 관련 원인을 배제할 수 없음: '+sentence.strip())
    allowed=observation_numbers(collected)
    for value in re.findall(r'(\d+(?:\.\d+)?)\s*%',draft):
        if float(value) not in allowed:
            errors.append(f'근거에 없는 백분율 {value}%')
    # 의미상의 세부 차이는 별도로 검토하고, 여기서는 낮은 메모리 사용률에서 명시적으로 재시작을 권고하는 문장을 검사한다.
    low=[c['data']['memory_percent'] for c in collected if c['source']=='monitoring'
         and c.get('target')=='cache-service' and 'memory_percent' in c['data']]
    if low and max(low)<90 and re.search(r'즉시\s*재시작.{0,12}(필요|해야|권장)',draft):
        if not re.search(r'즉시\s*재시작.{0,20}(않|아니|불필요)',draft):
            errors.append('90% 미만에서 즉시 재시작을 요구함')
    return list(dict.fromkeys(errors))
