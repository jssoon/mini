"""OPIC 스크립트 생성 에이전트가 사용하는 도메인 도구 모음.

data/ 폴더의 참고 데이터(주제 목록, 등급 기준표, 표현 사전)를 조회하고,
생성한 스크립트가 등급 분량과 키워드 조건을 지키는지 검사한다.
모든 도구는 에이전트가 읽기 쉽도록 JSON 문자열을 돌려준다.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from src.retriever import DOC_TYPES, retrieve, to_contexts

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEVEL_ORDER = ["IM1", "IM2", "IM3", "IH", "AL"]
ALL_TYPES = "전체"


# ---------------------------------------------------------------------------
# 내부 함수
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    """data/ 폴더의 JSON 파일을 읽어 딕셔너리로 돌려준다. 한 번 읽은 파일은 캐시한다."""
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _to_json(data: dict) -> str:
    """도구 결과를 한글이 깨지지 않는 JSON 문자열로 바꾼다."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _normalize_level(level: str) -> Optional[str]:
    """입력한 등급을 표준 표기(IM1~AL)로 바꾼다. 유효하지 않으면 None을 돌려준다."""
    value = (level or "").strip().upper().replace(" ", "")
    return value if value in LEVEL_ORDER else None


def _invalid_level(level: str) -> str:
    """잘못된 등급이 들어왔을 때 돌려줄 오류 메시지를 만든다."""
    return _to_json({
        "error": f"지원하지 않는 등급입니다: {level}",
        "valid_levels": LEVEL_ORDER,
    })


def _count_sentences(text: str) -> int:
    """마침표·물음표·느낌표를 기준으로 문장 수를 센다. 말줄임표(...)는 한 번으로 본다."""
    parts = re.split(r"[.!?]+(?:\s+|$)", text.strip())
    return len([p for p in parts if re.search(r"[A-Za-z]", p)])


def _count_words(text: str) -> int:
    """영어 단어 수를 센다. 축약형(I'm, don't)은 한 단어로 본다."""
    return len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))


# ---------------------------------------------------------------------------
# 도구
# ---------------------------------------------------------------------------

@tool
def list_topics(category: Optional[str] = None) -> str:
    """OPIC 주제 목록을 조회한다.

    사용자가 어떤 주제가 있는지 묻거나, 요청한 주제가 목록의 어느 항목에
    해당하는지 확인할 때 사용한다.

    Args:
        category: 주제 분류. 자기소개, 직업, 거주 형태, 여가, 취미, 운동, 여행, 돌발 중 하나.
            비워 두면 전체 주제를 돌려준다.
    """
    data = _load("topics.json")
    topics = data["topics"]
    if category:
        topics = [t for t in topics if t["category"] == category.strip()]
        if not topics:
            return _to_json({
                "error": f"없는 분류입니다: {category}",
                "valid_categories": [c["category"] for c in data["categories"]],
            })
    return _to_json({
        "count": len(topics),
        "topics": [
            {"id": t["id"], "name_en": t["name_en"], "question_types": t["question_types"]}
            for t in topics
        ],
    })


@tool
def get_topic_info(topic: str) -> str:
    """주제 하나의 상세 정보(예시 질문, 문제 유형, 주제 어휘)를 조회한다.

    스크립트를 쓰기 전에 주제에 맞는 질문 형태와 어휘를 확인할 때 사용한다.
    정확한 이름이 아니어도 일부만 맞으면 후보를 찾아 준다(예: "공원" → "여가-공원 가기").

    Args:
        topic: 주제 이름. 예: "여가-공원 가기", "공원", "Jogging".
    """
    query = (topic or "").strip().lower()
    if not query:
        return _to_json({"error": "주제 이름을 입력해야 합니다."})

    topics = _load("topics.json")["topics"]
    aliases = lambda t: [a.lower() for a in t.get("aliases", [])]  # noqa: E731
    exact = [
        t for t in topics
        if query in (t["id"].lower(), t["name_en"].lower()) or query in aliases(t)
    ]
    if exact:
        return _to_json(exact[0])

    candidates = [
        t for t in topics
        if query in t["id"].lower() or query in t["name_en"].lower()
        or any(query in a or a in query for a in aliases(t))
    ]
    if len(candidates) == 1:
        return _to_json(candidates[0])
    if candidates:
        # 다시 조회하지 않아도 되도록 후보의 상세 정보를 모두 돌려준다.
        return _to_json({
            "message": "여러 주제가 해당됩니다. 요청에 가장 맞는 주제를 골라 쓰세요. 다시 조회할 필요는 없습니다.",
            "candidates": candidates[:5],
        })
    return _to_json({
        "error": f"목록에 없는 주제입니다: {topic}",
        "hint": "list_topics로 전체 목록을 확인하거나, 목록에 없는 주제라면 일반 지식으로 스크립트를 작성하세요.",
    })


@tool
def get_level_criteria(level: str, question_type: Optional[str] = None) -> str:
    """목표 등급의 답변 기준(문장 수, 단어 수, 문장 구조, 어휘, 시제, 연결어)을 조회한다.

    스크립트를 쓰기 전에 반드시 호출해 분량과 난이도를 맞춘다.
    문제 유형을 함께 주면 그 유형의 작성 가이드(중심 내용, 시제, 핵심 패턴)도 돌려준다.
    콤보 세트처럼 유형이 여러 개면 쉼표로 이어서 한 번에 조회한다 (예: "묘사, 과거 경험, 비교").

    Args:
        level: 목표 등급. IM1, IM2, IM3, IH, AL 중 하나.
        question_type: 문제 유형. 묘사, 루틴, 과거 경험, 비교,
            롤플레이(질문하기), 롤플레이(문제 해결) 중 하나 또는 쉼표로 이은 여러 개. 생략 가능.
    """
    normalized = _normalize_level(level)
    if not normalized:
        return _invalid_level(level)

    data = _load("levels.json")
    criteria = next(item for item in data["levels"] if item["level"] == normalized)
    result = {"criteria": criteria, "disclaimer": data["meta"]["disclaimer"]}

    if question_type:
        guides = data["question_type_guides"]
        found, unknown = [], []
        for qt in (q.strip() for q in question_type.split(",") if q.strip()):
            if qt in guides:
                found.append({"question_type": qt, **guides[qt]})
            else:
                unknown.append(qt)
        if len(found) == 1:
            result["question_type_guide"] = found[0]
        elif found:
            result["question_type_guides"] = found
        if unknown:
            result["question_type_warning"] = {
                "message": f"없는 문제 유형입니다: {', '.join(unknown)}",
                "valid_question_types": list(guides),
            }
    return _to_json(result)


@tool
def search_expressions(
    level: str,
    question_type: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 10,
) -> str:
    """목표 등급과 문제 유형에 맞는 영어 표현·필러를 표현 사전에서 찾는다.

    목표 등급 이하에서 쓸 수 있는 표현만 돌려주므로, 스크립트 난이도가 등급을 넘지 않게 한다.
    결과의 표현을 스크립트에 자연스럽게 섞어 쓴다.

    Args:
        level: 목표 등급. IM1, IM2, IM3, IH, AL 중 하나.
        question_type: 문제 유형. 비워 두면 모든 유형의 표현을 찾는다.
        category: 표현 분류. 필러, 도입, 묘사, 루틴, 과거 경험, 비교, 롤플레이,
            연결어, 감정·평가, 마무리 등. 앞부분만 맞아도 된다(예: "필러").
        limit: 돌려줄 최대 개수. 기본 10개.
    """
    normalized = _normalize_level(level)
    if not normalized:
        return _invalid_level(level)

    max_rank = LEVEL_ORDER.index(normalized)
    results = []
    for item in _load("expressions.json")["expressions"]:
        if LEVEL_ORDER.index(item["min_level"]) > max_rank:
            continue
        if question_type and not (
            question_type in item["question_types"] or ALL_TYPES in item["question_types"]
        ):
            continue
        if category and not item["category"].startswith(category.strip()):
            continue
        results.append(item)

    # 해당 문제 유형 전용 표현을 공통 표현보다 먼저, 같은 조건이면 목표 등급에 가까운 표현을 먼저 보여 준다.
    results.sort(key=lambda x: (
        bool(question_type) and question_type in x["question_types"],
        LEVEL_ORDER.index(x["min_level"]),
    ), reverse=True)
    limit = max(1, min(int(limit), 30))
    return _to_json({
        "count": len(results[:limit]),
        "total_matched": len(results),
        "expressions": [
            {k: item[k] for k in ("id", "category", "expression", "meaning_ko", "example")}
            for item in results[:limit]
        ],
    })


@tool
def get_roleplay_scenarios(
    roleplay_type: Optional[str] = None,
    topic: Optional[str] = None,
) -> str:
    """롤플레이 문항의 상황 설정과 예시 질문을 조회한다.

    롤플레이(질문하기) 또는 롤플레이(문제 해결) 스크립트를 쓸 때 상황을 정하기 위해 사용한다.

    Args:
        roleplay_type: "롤플레이(질문하기)" 또는 "롤플레이(문제 해결)". 비워 두면 둘 다 찾는다.
        topic: 관련 주제 이름 또는 그 일부. 예: "영화", "여행-해외 여행".
    """
    scenarios = _load("topics.json")["roleplay_scenarios"]
    if roleplay_type:
        scenarios = [s for s in scenarios if s["type"] == roleplay_type.strip()]
    if topic:
        scenarios = [s for s in scenarios if topic.strip() in s["related_topic"]]
    if not scenarios:
        return _to_json({
            "error": "조건에 맞는 롤플레이 상황이 없습니다.",
            "hint": "조건을 줄여 다시 조회하거나, 주제에 맞는 상황을 직접 설정해 작성하세요.",
        })
    return _to_json({"count": len(scenarios), "scenarios": scenarios})


@tool
def check_script(script: str, level: str, keywords: Optional[list[str]] = None) -> str:
    """작성한 영어 스크립트가 등급 분량과 키워드 조건을 지키는지 검사한다.

    스크립트를 사용자에게 보여 주기 전에 반드시 호출한다.
    passed가 false이면 issues를 보고 스크립트를 고친 뒤 한 번 더 검사한다(재검사는 최대 2회).

    Args:
        script: 검사할 영어 스크립트 본문. 한국어 해석과 핵심 표현 정리는 빼고 넣는다.
        level: 목표 등급. IM1, IM2, IM3, IH, AL 중 하나.
        keywords: 스크립트에 들어가야 할 개인 경험 키워드를, 스크립트에 실제로 쓴
            영어 표현으로 넣는다. 예: ["15th floor", "Han River", "two cats"].
    """
    normalized = _normalize_level(level)
    if not normalized:
        return _invalid_level(level)

    criteria = next(
        item for item in _load("levels.json")["levels"] if item["level"] == normalized
    )
    min_sent, max_sent = criteria["length"]["sentences"]
    min_word, max_word = criteria["length"]["words"]

    sentences = _count_sentences(script)
    words = _count_words(script)
    lowered = script.lower()
    keywords = [k.strip() for k in (keywords or []) if k and k.strip()]
    missing = [k for k in keywords if k.lower() not in lowered]

    issues = []
    if not min_sent <= sentences <= max_sent:
        issues.append(f"문장 수 {sentences}개: {normalized} 기준 {min_sent}~{max_sent}개를 벗어났습니다.")
    if not min_word <= words <= max_word:
        issues.append(f"단어 수 {words}개: {normalized} 기준 {min_word}~{max_word}개를 벗어났습니다.")
    if missing:
        issues.append(f"빠진 키워드: {', '.join(missing)}")

    # 가드레일: 연락처·이메일 같은 민감 정보가 스크립트에 들어가지 않게 한다.
    if re.search(r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b", script):
        issues.append("전화번호로 보이는 정보가 들어 있습니다. 삭제하세요.")
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", script):
        issues.append("이메일 주소가 들어 있습니다. 삭제하세요.")

    return _to_json({
        "passed": not issues,
        "level": normalized,
        "sentence_count": sentences,
        "word_count": words,
        "expected_sentences": [min_sent, max_sent],
        "expected_words": [min_word, max_word],
        "keyword_coverage": {
            "total": len(keywords),
            "included": len(keywords) - len(missing),
            "missing": missing,
        },
        "issues": issues,
    })


@tool
def search_references(
    query: str,
    level: Optional[str] = None,
    question_type: Optional[str] = None,
    doc_types: Optional[list[str]] = None,
    k: int = 5,
) -> str:
    """참고 데이터 전체에서 질문과 의미가 비슷한 자료를 검색한다(RAG).

    주제 이름이 목록과 정확히 맞지 않거나, "비 때문에 계획이 틀어진 경험"처럼
    상황·내용으로 관련 주제·표현·롤플레이 상황을 찾고 싶을 때 사용한다.

    Args:
        query: 검색할 내용. 예: "한강에서 자전거 탄 경험", "호텔 예약 문제 해결 표현".
        level: 목표 등급(IM1~AL). 주면 그 등급 이하의 표현과 그 등급의 기준만 찾는다.
        question_type: 문제 유형. 주면 그 유형에 맞는 자료만 찾는다.
        doc_types: 찾을 자료 종류. topic, roleplay, level, question_type_guide, expression 중에서 고른다.
            비워 두면 모든 종류에서 찾는다.
        k: 돌려줄 자료 수. 기본 5개, 최대 10개.
    """
    if level and not _normalize_level(level):
        return _invalid_level(level)
    if doc_types:
        invalid = [d for d in doc_types if d not in DOC_TYPES]
        if invalid:
            return _to_json({"error": f"없는 자료 종류입니다: {invalid}", "valid_doc_types": DOC_TYPES})

    try:
        docs = retrieve(
            query,
            k=max(1, min(int(k), 10)),
            doc_types=doc_types,
            level=_normalize_level(level) if level else None,
            question_type=question_type.strip() if question_type else None,
        )
    except Exception as e:  # Bedrock 연결 실패 등. 에이전트가 다른 도구로 계속 진행하게 한다.
        return _to_json({
            "error": f"검색 중 오류가 발생했습니다: {type(e).__name__}",
            "hint": "get_topic_info, search_expressions 등 다른 조회 도구를 사용하세요.",
        })
    return _to_json({"count": len(docs), "contexts": to_contexts(docs)})


# 에이전트에 등록할 도구 목록
TOOLS = [
    list_topics,
    get_topic_info,
    get_level_criteria,
    search_expressions,
    get_roleplay_scenarios,
    search_references,
    check_script,
]
