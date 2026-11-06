"""evaluation/test_queries.csv의 평가 케이스를 pytest로 실행한다.

실행 (프로젝트 루트에서. Bedrock을 실제로 호출하므로 .env의 AWS 자격 증명이 필요하다)
    pytest tests/ -v                      # 전체 35개 (약 10분)
    pytest tests/ -v -k TQ021             # 질의 하나만
    pytest tests/ -v -k "TQ02 or TQ03"    # 여러 개
    pytest tests/ -v -k "not latency"     # 응답 시간 검사는 빼고
    $env:QUESTION="..."; pytest tests/ -k ask -s   # 직접 입력한 질문의 답변 보기

채점 기준은 evaluation/run_eval.py의 evaluate()를 그대로 쓴다.
질의마다 에이전트를 한 번만 실행하고, 그 결과로 아래 네 가지를 따로 검사한다.
    test_behavior  기대 동작 (거절·생성·확인 질문·경고 안내)
    test_length    등급별 문장·단어 수
    test_keywords  개인 경험 키워드 반영
    test_latency   응답 25초 이내 (SERVICE.md 속도 기준)
"""

import os
import re
import sys
from datetime import datetime
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


def test_ask():
    """환경 변수 QUESTION에 넣은 질문으로 에이전트를 실행하고 답변을 출력한다.

    PowerShell:  $env:QUESTION="카페 묘사 스크립트 IH로"; pytest tests/ -k ask -s
    끝나면:      Remove-Item Env:QUESTION
    """
    question = os.getenv("QUESTION")
    if not question:
        pytest.skip("QUESTION 환경 변수가 없음")
    result = run_eval.agent.run(question)
    print(f"\n질문: {question}\n\n{result['answer']}")
    assert result["answer"]


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


def _add_runs(paragraph, text: str) -> None:
    """**굵게** 표시를 살려 문단에 글자를 넣는다."""
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            paragraph.add_run(part.replace("*", "")).bold = i % 2 == 1


def save_docx(items: list[tuple[str, str]]) -> Path:
    """(질문, 답변) 목록을 워드 파일(.docx) 하나에 순서대로 저장하고 경로를 돌려준다."""
    from docx import Document

    doc = Document()
    doc.add_heading("OPIC 연습 스크립트", level=1)
    for n, (question, answer) in enumerate(items, 1):
        if n > 1:
            doc.add_page_break()
        doc.add_heading(f"{n}. {question}", level=1)
        _add_answer_docx(doc, answer)

    path = Path.cwd() / f"opic_script_{datetime.now():%Y%m%d_%H%M%S}.docx"
    doc.save(path)
    return path


def _add_answer_docx(doc, answer: str) -> None:
    """답변(마크다운) 하나를 워드 문서에 제목·문단·표로 넣는다."""
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        """모아 둔 마크다운 표를 워드 표로 넣는다."""
        if table_rows:
            table = doc.add_table(rows=0, cols=len(table_rows[0]))
            table.style = "Table Grid"
            for cells in table_rows:
                row = table.add_row().cells
                for cell, text in zip(row, cells):
                    _add_runs(cell.paragraphs[0], text)
            table_rows.clear()

    for line in answer.splitlines():
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", c) for c in cells):  # 구분선(|---|) 제외
                table_rows.append(cells)
            continue
        flush_table()
        if not s or s == "---":
            continue
        if s.startswith("#"):
            doc.add_heading(s.lstrip("#").strip(), level=2)
        elif s.startswith(">"):
            _add_runs(doc.add_paragraph(style="Quote"), s.lstrip("> "))
        elif s.startswith(("- ", "* ")):
            _add_runs(doc.add_paragraph(style="List Bullet"), s[2:])
        else:
            _add_runs(doc.add_paragraph(), s)
    flush_table()


def save_txt(items: list[tuple[str, str]]) -> Path:
    """(질문, 답변) 목록을 마크다운 기호(#, **, |---|)를 정리해 텍스트 파일(.txt) 하나에 저장하고 경로를 돌려준다."""
    lines = ["OPIC 연습 스크립트"]
    for n, (question, answer) in enumerate(items, 1):
        lines += ["", "=" * 60, f"{n}. {question}", "=" * 60]
        lines += _answer_txt_lines(answer)
    path = Path.cwd() / f"opic_script_{datetime.now():%Y%m%d_%H%M%S}.txt"
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _answer_txt_lines(answer: str) -> list[str]:
    """답변(마크다운) 하나를 텍스트 줄 목록으로 바꾼다."""
    lines: list[str] = []
    for line in answer.splitlines():
        s = line.strip()
        if s == "---" or re.fullmatch(r"\|?[\s:|-]+\|?", s or "x"):
            continue
        if s.startswith("#"):
            s = f"[{s.lstrip('#').strip()}]"
            lines.append("")
        elif s.startswith("|"):
            s = " - ".join(c.strip() for c in s.strip("|").split("|"))
        lines.append(s.lstrip("> ").replace("**", "").replace("*", ""))
    return lines


if __name__ == "__main__":
    # 직접 실행하면 질문을 입력받아 답변을 출력한다. 빈 줄이나 q를 입력하면 끝난다.
    # 끝낼 때 그동안 받은 답변을 워드(.docx)나 텍스트(.txt) 파일 하나로 한꺼번에 저장할지 묻는다.
    #   .venv\Scripts\python tests\test_eval.py
    history: list[tuple[str, str]] = []
    while True:
        q = input("\n질문 (끝내려면 q): ").strip()
        if not q or q.lower() == "q":
            break
        print("\n생성 중...\n", flush=True)
        answer = run_eval.agent.run(q)["answer"]
        print(answer)
        history.append((q, answer))

    if history:
        choice = input(f"\n답변 {len(history)}개를 한 파일로 저장할까요? "
                       "(1: 워드 .docx / 2: 텍스트 .txt / Enter: 저장 안 함): ").strip()
        if choice in ("1", "2"):
            print(f"저장했습니다: {(save_docx if choice == '1' else save_txt)(history)}")
