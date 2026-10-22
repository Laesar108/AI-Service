"""all_total 통합 실습 — MCP 서버 (단계 2 축).

도구 / 리소스 / 프롬프트의 세 가지 기능을 모두 노출하는 실제 fastmcp 서버.
FastAPI 백엔드(server.py)가 lifespan에서 이 파일을 subprocess(stdio)로 1회 기동해
프로세스가 살아 있는 동안 재사용한다 (단계 1 싱글턴 + 단계 2 표준화의 결합).

- 도구 get_monitoring_status : 호출마다 값이 달라지는 동적 조회이므로 도구로 제공
- 리소스 runbook://incident-response/{incident_type} : URI로 식별되는 정적 문서
- 프롬프트 incident_analysis_prompt : 여러 도구를 조합하는 표준 진단 절차를 지정
"""
import json
import random
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel, Field

mcp = FastMCP(name="capstone-all-total-ops")

DATA_DIR = Path(__file__).parent / "data"


class SystemStatus(BaseModel):
    # [자료 구분] 실제 운영 측정값으로 오인하지 않도록 모의 데이터임을 응답에 명시한다.
    data_kind: str = Field(default="synthetic", description="실습용 모의 데이터")
    system_name: str = Field(description="시스템 이름")
    status: str = Field(description="상태: healthy / degraded / down")
    cpu_percent: float = Field(description="CPU 사용률 (%)")
    memory_percent: float = Field(description="메모리 사용률 (%)")
    last_checked: str = Field(description="마지막 체크 시각 (ISO 8601)")
    alerts: list[str] = Field(default_factory=list, description="활성 알림 목록")


@mcp.tool()
def get_monitoring_status(system_name: str) -> SystemStatus:
    """지정된 시스템의 현재 모니터링 상태를 조회합니다 (호출마다 값이 달라지는 동적 도구)."""
    monitoring_data = json.loads((DATA_DIR / "monitoring.json").read_text())
    if system_name not in monitoring_data:
        return SystemStatus(
            system_name=system_name, status="unknown",
            cpu_percent=0.0, memory_percent=0.0,
            last_checked=datetime.now().isoformat(),
            alerts=[f"System '{system_name}' not found in monitoring"],
        )
    data = monitoring_data[system_name]
    jitter = random.uniform(-3.0, 8.0)
    return SystemStatus(
        system_name=system_name,
        status=data["status"],
        cpu_percent=round(data["cpu_base"] + jitter, 1),
        memory_percent=round(data["memory_base"] + jitter / 2, 1),
        last_checked=datetime.now().isoformat(timespec="microseconds"),
        alerts=data.get("alerts", []),
    )


@mcp.resource("runbook://incident-response/{incident_type}")
def get_incident_runbook(incident_type: str) -> str:
    """특정 장애 유형의 대응 매뉴얼(정적 문서)을 반환합니다."""
    runbook_path = DATA_DIR / "runbooks" / f"{incident_type}.md"
    if not runbook_path.exists():
        return f"# Runbook Not Found\n\nNo runbook for incident type: {incident_type}"
    return runbook_path.read_text()


# [진단 근거 수집] 현재 지표만으로 로그/추세를 추정하지 않도록 별도 진단 근거 도구를 추가했다.
# 현재 소스는 모의 JSON이며 not_provided는 정상 판정이 아닌 자료 미제공이다.
@mcp.tool()
def get_diagnostic_evidence(system_name: str) -> dict:
    """명시적 모의 진단 자료 조회. 자료 미제공은 정상 또는 이상 없음이 아니다."""
    registry = json.loads((DATA_DIR / "monitoring.json").read_text())
    if system_name not in registry:
        raise ValueError("System not registered")
    path = DATA_DIR / "diagnostic_evidence.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    source = data.get(system_name, {})
    return {
        "system_name": system_name,
        "data_kind": "synthetic",
        **{kind: source.get(kind, {"availability": "not_provided", "records": []})
           for kind in ("logs", "cpu_series", "deployments")},
    }


@mcp.prompt()
def incident_analysis_prompt(system_name: str, symptom: str) -> str:
    """장애 진단 표준 절차 프롬프트 — 계획 노드가 계획을 세울 때 이 절차를 따른다."""
    return f"""다음 장애에 대해 표준 진단 절차를 수행하세요.

## 장애 정보
- 시스템: {system_name}
- 증상: {symptom}

## 진단 절차 (반드시 순서대로 수행)
1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.
2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.
3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.
"""


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()
