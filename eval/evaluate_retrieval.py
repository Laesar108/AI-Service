"""개발용 질문으로 검색 설정을 조정하고 고정한 뒤 별도로 분리한 질문으로 평가한다."""
import asyncio
import json
import logging
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent.retrieval import RunbookRetriever
from agent.policies import topics_for
from agent.telemetry import CALLS, STAGE


def metrics(rows):
    tp=sum(len(set(r["predicted"]) & set(r["expected"])) for r in rows)
    predicted=sum(len(r["predicted"]) for r in rows)
    expected=sum(len(r["expected"]) for r in rows)
    precision=tp/predicted if predicted else 0
    recall=tp/expected if expected else 0
    return {"precision":precision,"recall":recall,
            "f1":2*precision*recall/(precision+recall) if precision+recall else 0,
            "exact_match":sum(set(r["predicted"])==set(r["expected"]) for r in rows)/len(rows),
            "true_positive":tp,"predicted":predicted,"expected":expected,"cases":len(rows)}


# [검색 품질 평가] 개발 질문에서 검색 설정을 선택하고 분리된 평가 질문으로 검색 품질을 확인한다.
# 여기의 keyword 대조군은 규칙 검색기이며 최초 전체 그래프 Before와 동일한 비교가 아니다.
async def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/".env")
    logging.disable(logging.CRITICAL)
    path=ROOT/"eval/results/retrieval"
    path.mkdir(exist_ok=False)
    cases=json.loads((ROOT/"eval/retrieval_cases.json").read_text())
    calls=[];CALLS.set(calls);STAGE.set("index_build")
    retriever=RunbookRetriever()
    await retriever.ensure_index()
    (path/"index_usage.json").write_text(json.dumps(calls,indent=2))
    calls=[];CALLS.set(calls);STAGE.set("retrieval_calibration")
    dev=await retriever.rank([c["query"] for c in cases["development"]])
    candidates=[]
    for threshold in (0.25,0.30,0.35,0.40,0.45,0.50):
        for margin in (0.04,0.08,0.12):
            for k in (1,2,3):
                retriever.threshold,retriever.margin,retriever.top_k=threshold,margin,k
                rows=[{**c,"predicted":[d["id"] for d in retriever.select(r)]}
                      for c,r in zip(cases["development"],dev)]
                m=metrics(rows)
                candidates.append((m["f1"],m["exact_match"],-k,threshold,margin,k,m))
    best=max(candidates)
    retriever.threshold,retriever.margin,retriever.top_k=best[3:6]
    config={"threshold":best[3],"margin":best[4],"top_k":best[5],
            "model":retriever.embedder.model,"selection_split":"development","development_metrics":best[6]}
    (ROOT/"data/retrieval_config.json").write_text(json.dumps(config,indent=2))
    # 이 평가 결과를 보고 이후 검색 매개변수를 다시 선택하지 않는다.
    test=await retriever.rank([c["query"] for c in cases["test"]])
    output={"config":config,"splits":{}}
    for split, rankings in (("development",dev),("test",test)):
        rows=[]
        for c,ranking in zip(cases[split],rankings):
            rows.append({**c,"predicted":[d["id"] for d in retriever.select(ranking)],
                         "ranking":[{"id":d["id"],"score":d["score"]} for d in ranking],
                         "keyword":topics_for({"status":"degraded","alerts":[c["query"]]})})
        keyword_rows=[{**r,"predicted":r["keyword"]} for r in rows]
        output["splits"][split]={"rag":metrics(rows),"keyword":metrics(keyword_rows),"rows":rows}
    output["usage"]=calls
    (path/"results.json").write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps({s:{k:v for k,v in r.items() if k!="rows"} for s,r in output["splits"].items()},ensure_ascii=False))


if __name__=="__main__":
    try: asyncio.run(main())
    except Exception as exc:
        print("Retrieval evaluation stopped: "+type(exc).__name__);sys.exit(1)
