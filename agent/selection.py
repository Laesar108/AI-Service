"""질문 ID나 정답을 사용하지 않고 수집된 근거에 따라 전문 분석 역할을 선택한다."""


# [추가 최적화] 전문 분석 상시 호출을 자료 기반 선택으로 변경했다. 별도 선택 LLM은 없다.
# 서비스 수·자료 제공·구조화된 모순을 사용하며 평가 질문 ID/정답에 의존하지 않는다.
# 모든 의미적 복잡성을 판별하는 선택기는 아니므로 복잡한 질문을 놓칠 가능성은 남는다.
def research_selection(state):
    reasons = {"metrics": [], "logs": [], "runbook": []}
    metrics = [c["data"] for c in state.get("collected", [])
               if c["source"] == "monitoring" and not c.get("error")]
    if len(state.get("targets", [])) > 1:
        reasons["metrics"].append("여러 서비스의 관측 비교")
        reasons["runbook"].append("여러 서비스의 조치 적용 조건 비교")
    for item in state.get("collected", []):
        if item.get("error") or item["source"] != "evidence":
            continue
        data = item["data"]
        for kind in ("logs", "deployments", "cpu_series"):
            evidence = data.get(kind, {})
            if evidence.get("availability") == "provided" and evidence.get("records"):
                role = "metrics" if kind == "cpu_series" else "logs"
                reasons[role].append(f"{item['target']}: {kind} 자료 제공")
    # 정상 상태 표시와 알림 또는 높은 지표가 상충하면 추가 해석이 필요하다.
    for m in metrics:
        if m.get("status") == "healthy" and (m.get("alerts") or
                any(isinstance(m.get(k), (int, float)) and m[k] >= 90
                    for k in ("cpu_percent", "memory_percent"))):
            reasons["metrics"].append("정상 상태 표시와 관측 지표/알림이 상충")
            reasons["runbook"].append("상충 관측에 대한 조치 적용성 확인")
    # 같은 서비스에 대한 중복 관측값이 서로 다른 경우에도 비교가 필요하다.
    by_system = {}
    for m in metrics:
        values = (m.get("status"), m.get("cpu_percent"), m.get("memory_percent"))
        target = m.get("system_name")
        if target in by_system and by_system[target] != values:
            reasons["metrics"].append("동일 서비스의 관측 값이 상충")
            reasons["runbook"].append("상충 관측에 대한 조치 적용성 확인")
        by_system[target] = values
    return {role: {"enabled": bool(why), "reasons": list(dict.fromkeys(why))}
            for role, why in reasons.items()}


async def select_research_node(state):
    selection = research_selection(state)
    return {"research_selection": selection,
            "events": state.get("events", []) + [{"type": "ResearchSelected",
                "roles": [r for r, v in selection.items() if v["enabled"]]}]}
