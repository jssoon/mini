"""evaluation/test_queries.csv의 평가 케이스를 pytest로 실행한다.

실행 (프로젝트 루트에서. Bedrock을 실제로 호출하므로 .env의 AWS 자격 증명이 필요하다)
    pytest tests/ -v                      # 전체 35개 (약 10분)
    pytest tests/ -v -k TQ021             # 질의 하나만
    pytest tests/ -v -k "TQ02 or TQ03"    # 여러 개
    pytest tests/ -v -k "not latency"     # 응답 시간 검사는 빼고

채점 기준은 evaluation/run_eval.py의 evaluate()를 그대로 쓴다.
질의마다 에이전트를 한 번만 실행하고, 그 결과로 아래 네 가지를 따로 검사한다.
    test_behavior  기대 동작 (거절·생성·확인 질문·경고 안내)
    test_length    등급별 문장·단어 수
    test_keywords  개인 경험 키워드 반영
    test_latency   응답 25초 이내 (SERVICE.md 속도 기준)
"""

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))
load_dotenv(ROOT / ".env")

import run_eval  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.getenv("AWS_ACCESS_KEY_ID"),
    reason="AWS 자격 증명이 없어 Bedrock을 호출할 수 없습니다 (.env 확인)",
)

ROWS = run_eval.load_queries(None)
IDS = [r["id"] for r in ROWS]
_RESULTS: dict[str, dict] = {}


def _scored(row: dict) -> dict:
    """질의를 한 번만 실행해 채점 결과를 캐시하고 돌려준다. 실행 오류도 실패 결과로 캐시한다."""
    if row["id"] not in _RESULTS:
        _RESULTS[row["id"]] = run_eval.run_one(row)
    r = _RESULTS[row["id"]]
    if r["reasons"] and r["reasons"][0].startswith("실행 오류"):
        pytest.fail(f"[{r['id']}] {r['reasons'][0]}")
    return r


def _detail(r: dict) -> str:
    """실패 메시지에 붙일 요약 정보를 만든다."""
    return (f"[{r['id']}] {r['query']}\n  분류={r['label']} 시간={r['elapsed_sec']}초 "
            f"검사={r['check_script_runs']}회\n  이유: {'; '.join(r['reasons']) or '-'}")


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_behavior(row):
    """기대 동작(거절·생성·콤보 3개·확인 질문·경고 안내)과 일치하는지 검사한다."""
    r = _scored(row)
    assert r["behavior_pass"], _detail(r)


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_length(row):
    """스크립트가 목표 등급(미지정 시 IM3)의 문장·단어 수 범위 안에 있는지 검사한다."""
    r = _scored(row)
    if r["length_pass"] is None:
        pytest.skip("스크립트를 만들지 않는 질의")
    assert r["length_pass"], _detail(r) + f"\n  분량: {r['script_lengths']}"


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_keywords(row):
    """사용자가 준 개인 경험 키워드가 스크립트에 모두 들어갔는지 검사한다."""
    r = _scored(row)
    if r["keyword_pass"] is None:
        pytest.skip("키워드가 없는 질의")
    assert r["keyword_pass"], _detail(r)


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_latency(row):
    """응답이 SERVICE.md 기준인 25초 안에 왔는지 검사한다."""
    r = _scored(row)
    assert r["latency_pass"], _detail(r)
