import asyncio
import copy
import json
import unittest
from pathlib import Path
from agent.deps import SupervisorDeps
from agent.graph import build_graph
from agent.policies import identify_targets, topics_for, deterministic_errors
from eval.run_evaluation import FixtureMCP
from eval.validate_dataset import validate

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'eval/fixtures/v1'
DATA=validate(FIXTURE)


class Stub:
    def __init__(self, rejects=0, malformed=False):
        self.rejects=rejects;self.malformed=malformed;self.reviews=0
    def route(self,q):
        return {'route':'chat' if '이름' in q or '안녕' in q else 'incident'},{'usd':0}
    async def chat(self,q,history,on_token=None):
        answer='민수님입니다!' if '뭐였죠' in q else '안녕하세요'
        return answer,{'usd':0}
    def _chat(self,system,user,json_mode=False):
        if '운영 요청 분류' in system:
            return json.dumps({'route':'chat' if '이름' in user or '안녕' in user else 'incident'}),{'usd':0}
        if '간결한 한국어 비서' in system:
            return '민수님입니다!' if '뭐였죠' in user else '안녕하세요',{'usd':0}
        if json_mode:
            self.reviews+=1
            if self.malformed:return 'not json',{'usd':0}
            errors=['unsupported'] if self.reviews<=self.rejects else []
            return json.dumps({'errors':errors,'passed':not errors}),{'usd':0}
        return '1) 모의 데이터입니다. 2) 자료 미확인. 3) 추가 확인 필요.',{'usd':0}


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self,idx,stub=None,mcp=None):
        case=DATA['cases'][idx];stub=stub or Stub();mcp=mcp or FixtureMCP(DATA,case,FIXTURE)
        app=build_graph(SupervisorDeps(stub,stub,mcp,None,'v2'))
        result=await app.ainvoke({'query':case['turns'][0],'history':[],'events':[],'cost_usd':0})
        return result,mcp
    async def test_unknown_target_never_queries_other_service(self):
        result,mcp=await self.execute(7)
        self.assertEqual(mcp.calls,[]);self.assertIn('billing-service',result['final'])
    async def test_multi_issue_loads_both_runbooks(self):
        result,mcp=await self.execute(3)
        resources=[c['resource'] for c in mcp.calls if 'resource' in c]
        self.assertEqual(set(resources),{'runbook://incident-response/cpu-spike','runbook://incident-response/network-timeout'})
        self.assertEqual(result['replan_rounds'],1)
    async def test_reject_then_pass(self):
        result,_=await self.execute(0,Stub(rejects=1))
        self.assertEqual(result['validation_attempts'],2)
        self.assertEqual(result['validation_status'],'passed')
    async def test_exhaustion_never_auto_passes(self):
        result,_=await self.execute(0,Stub(rejects=100))
        self.assertEqual(result['validation_attempts'],2)
        self.assertEqual(result['validation_status'],'limited')
        self.assertIn('검증을 완료하지 못해',result['final'])
    async def test_malformed_review_fails_closed(self):
        result,_=await self.execute(0,Stub(malformed=True))
        self.assertEqual(result['validation_status'],'limited')
    async def test_tool_failure_is_unknown_not_normal(self):
        class Broken(FixtureMCP):
            async def call_tool(self,*args,**kw): raise ValueError('unavailable')
        result,_=await self.execute(0,mcp=Broken(DATA,DATA['cases'][0],FIXTURE))
        self.assertTrue(any('조회 실패' in g for g in result['missing_evidence']))
        self.assertLessEqual(result['replan_rounds'],1)
    async def test_missing_runbook_does_not_loop(self):
        class Broken(FixtureMCP):
            async def read_resource(self,*args,**kw): return '# Runbook Not Found'
        result,_=await self.execute(0,mcp=Broken(DATA,DATA['cases'][0],FIXTURE))
        self.assertEqual(result['replan_rounds'],1)
        self.assertTrue(any('runbook 조회 실패' in g for g in result['missing_evidence']))
    async def test_login_assumption(self):
        result,_=await self.execute(6)
        self.assertIn('가정',result['assumption'])
    async def test_chat_skips_research(self):
        _,mcp=await self.execute(8)
        self.assertEqual(mcp.calls,[])
    async def test_boundary_calculated_in_code(self):
        for idx,expected in [(12,False),(13,True),(14,True)]:
            result,_=await self.execute(idx)
            self.assertEqual(result['analysis_summary']['systems']['cache-service']['memory_at_least_90'],expected)
    async def test_provenance_and_assumption_cannot_be_omitted(self):
        result,_=await self.execute(6)
        self.assertIn('실습용 모의 데이터',result['final'])
        self.assertIn('가정해 조사',result['final'])
    async def test_series_and_logs_are_distinct_evidence(self):
        result,_=await self.execute(10)
        facts=result['analysis_summary']['systems']['payments-service']
        self.assertEqual(facts['cpu_series_sample_count'],11)
        self.assertFalse(facts['logs_provided'])
        result,_=await self.execute(11)
        facts=result['analysis_summary']['systems']['payments-service']
        self.assertEqual(facts['cpu_series_sample_count'],0)
        self.assertTrue(facts['logs_provided'])


class PolicyTests(unittest.TestCase):
    def test_snapshot_tool_cannot_fetch_historical_evidence(self):
        self.assertTrue(deterministic_errors('get_monitoring_status로 CPU 지속 여부와 배포 이력을 확인한다.',[]))
        self.assertFalse(deterministic_errors('get_monitoring_status는 배포 이력을 제공하지 않습니다.',[]))
        self.assertFalse(deterministic_errors('외부 모니터링에서 시계열을 확인한다.',[]))
    def test_low_memory_does_not_exclude_cause(self):
        self.assertTrue(deterministic_errors('메모리 사용률이 낮으므로 메모리 문제를 배제한다.',[]))
        self.assertFalse(deterministic_errors('메모리 사용률이 낮아도 메모리 문제를 배제할 수 없다.',[]))
    def test_unknown_does_not_match_known_suffix(self):
        targets,unknown,_=identify_targets('fake-auth-service 응답이 느려요')
        self.assertEqual(targets,[]);self.assertEqual(unknown,['fake-auth-service'])
    def test_no_generic_default_target(self):
        self.assertEqual(identify_targets('응답 느림')[0],[])
    def test_multi_memory_cpu(self):
        self.assertEqual(set(topics_for({'status':'degraded','alerts':['CPU 90%, heap near limit']})),{'cpu-spike','memory-leak'})
    def test_percentage_not_in_evidence(self):
        facts=[{'source':'monitoring','target':'cache-service','data':{'memory_percent':88.7}}]
        self.assertTrue(deterministic_errors('메모리 99%',facts))
        self.assertFalse(deterministic_errors('메모리 88.7%',facts))
    def test_threshold_violation_and_negation(self):
        facts=[{'source':'monitoring','target':'cache-service','data':{'memory_percent':88.7}}]
        self.assertTrue(deterministic_errors('즉시 재시작이 필요합니다',facts))
        self.assertFalse(deterministic_errors('즉시 재시작이 필요하지 않습니다',facts))
    def test_expected_answers_not_sent_to_adapter(self):
        mcp=FixtureMCP(DATA,DATA['cases'][0],FIXTURE)
        result=asyncio.run(mcp.call_tool('get_monitoring_status',{'system_name':'payments-service'}))
        self.assertNotIn('expected',result)
        self.assertEqual(result['cpu_percent'],94.6)


if __name__=='__main__':unittest.main()
