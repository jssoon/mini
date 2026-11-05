"""OPIC 참고 데이터 RAG 파이프라인.

data/ 폴더의 주제 목록, 등급 기준표, 표현 사전, 롤플레이 상황을 문서(Document)로 바꾸고,
Amazon Bedrock 임베딩으로 벡터 저장소를 만들어 질문과 비슷한 문서를 찾는다.

환경 변수 (.env)
    AWS_REGION                  Bedrock 리전 (없으면 AWS_DEFAULT_REGION, 그것도 없으면 us-east-1)
    BEDROCK_EMBEDDING_MODEL_ID  임베딩 모델 ID (기본값: amazon.titan-embed-text-v2:0)
    AWS 인증 정보는 boto3 표준 방식(환경 변수 또는 AWS 프로필)으로 읽는다.
"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

load_dotenv()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEVEL_ORDER = ["IM1", "IM2", "IM3", "IH", "AL"]
DOC_TYPES = ["topic", "roleplay", "level", "question_type_guide", "expression"]
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
DEFAULT_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# 1단계: 데이터 → 문서
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    """data/ 폴더의 JSON 파일을 읽어 딕셔너리로 돌려준다."""
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _topic_documents() -> list[Document]:
    """주제 목록과 롤플레이 상황을 문서로 만든다."""
    data = _load("topics.json")
    docs = []
    for t in data["topics"]:
        questions = "\n".join(f"- {qt}: {q}" for qt, q in t["sample_questions"].items())
        content = (
            f"주제: {t['id']} ({t['name_en']})\n"
            f"분류: {t['category']}\n"
            f"문제 유형: {', '.join(t['question_types'])}\n"
            f"예시 질문:\n{questions}\n"
            f"주제 어휘: {', '.join(t['vocabulary'])}"
        )
        docs.append(Document(
            page_content=content,
            metadata={
                "doc_type": "topic",
                "source": "topics.json",
                "id": t["id"],
                "category": t["category"],
                "question_types": t["question_types"],
            },
        ))
    for s in data["roleplay_scenarios"]:
        content = (
            f"롤플레이 상황: {s['situation']}\n"
            f"유형: {s['type']}\n"
            f"관련 주제: {s['related_topic']}\n"
            f"예시 질문: {s['sample_question']}"
        )
        docs.append(Document(
            page_content=content,
            metadata={
                "doc_type": "roleplay",
                "source": "topics.json",
                "id": s["id"],
                "question_types": [s["type"]],
                "related_topic": s["related_topic"],
            },
        ))
    return docs


def _level_documents() -> list[Document]:
    """등급별 기준과 문제 유형별 작성 가이드를 문서로 만든다."""
    data = _load("levels.json")
    docs = []
    for lv in data["levels"]:
        length = lv["length"]
        content = (
            f"등급: {lv['level']} - {lv['summary']}\n"
            f"분량: {length['sentences'][0]}~{length['sentences'][1]}문장, "
            f"{length['words'][0]}~{length['words'][1]}단어\n"
            f"문장 구조: {' / '.join(lv['sentence_structure'])}\n"
            f"어휘: {' / '.join(lv['vocabulary'])}\n"
            f"시제: {' / '.join(lv['tense'])}\n"
            f"연결어: {', '.join(lv['connectors'])}\n"
            f"구성: {lv['structure_guide']}\n"
            f"팁: {' / '.join(lv['tips'])}\n"
            f"예문: {lv['sample_sentence']}"
        )
        docs.append(Document(
            page_content=content,
            metadata={"doc_type": "level", "source": "levels.json", "id": lv["level"], "level": lv["level"]},
        ))
    for qt, guide in data["question_type_guides"].items():
        content = (
            f"문제 유형 가이드: {qt}\n"
            f"중심 내용: {guide['focus']}\n"
            f"주요 시제: {guide['main_tense']}\n"
            f"핵심 패턴: {' / '.join(guide['key_patterns'])}"
        )
        docs.append(Document(
            page_content=content,
            metadata={
                "doc_type": "question_type_guide",
                "source": "levels.json",
                "id": qt,
                "question_types": [qt],
            },
        ))
    return docs


def _expression_documents() -> list[Document]:
    """표현 사전의 표현 하나하나를 문서로 만든다."""
    docs = []
    for ex in _load("expressions.json")["expressions"]:
        content = (
            f"표현: {ex['expression']} ({ex['meaning_ko']})\n"
            f"분류: {ex['category']}\n"
            f"쓰이는 유형: {', '.join(ex['question_types'])}\n"
            f"예문: {ex['example']}"
        )
        docs.append(Document(
            page_content=content,
            metadata={
                "doc_type": "expression",
                "source": "expressions.json",
                "id": ex["id"],
                "category": ex["category"],
                "question_types": ex["question_types"],
                "min_level": ex["min_level"],
            },
        ))
    return docs


def build_documents() -> list[Document]:
    """data/ 폴더의 모든 참고 데이터를 검색용 문서 목록으로 만든다."""
    return _topic_documents() + _level_documents() + _expression_documents()


# ---------------------------------------------------------------------------
# 2단계: 임베딩 → 벡터 저장소
# ---------------------------------------------------------------------------

def get_embeddings() -> Embeddings:
    """환경 변수에 설정된 Bedrock 임베딩 모델을 만든다."""
    from langchain_aws import BedrockEmbeddings

    return BedrockEmbeddings(
        model_id=os.getenv("BEDROCK_EMBEDDING_MODEL_ID", DEFAULT_EMBEDDING_MODEL_ID),
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_REGION,
    )


def build_vector_store(embeddings: Optional[Embeddings] = None) -> InMemoryVectorStore:
    """참고 데이터 문서를 임베딩해 메모리 벡터 저장소를 만든다.

    embeddings를 넘기지 않으면 Bedrock 임베딩을 사용한다. 테스트에서는 가짜 임베딩을 넘길 수 있다.
    """
    store = InMemoryVectorStore(embedding=embeddings or get_embeddings())
    docs = build_documents()
    store.add_documents(docs, ids=[f"{d.metadata['doc_type']}:{d.metadata['id']}" for d in docs])
    return store


@lru_cache(maxsize=1)
def get_vector_store() -> InMemoryVectorStore:
    """Bedrock 임베딩으로 만든 벡터 저장소를 한 번만 만들어 재사용한다."""
    return build_vector_store()


# ---------------------------------------------------------------------------
# 3단계: 검색
# ---------------------------------------------------------------------------

def _make_filter(
    doc_types: Optional[list[str]],
    level: Optional[str],
    question_type: Optional[str],
):
    """검색 조건(문서 종류, 목표 등급, 문제 유형)에 맞는 문서만 통과시키는 필터를 만든다."""
    max_rank = LEVEL_ORDER.index(level) if level in LEVEL_ORDER else None

    def _filter(doc: Document) -> bool:
        meta = doc.metadata
        if doc_types and meta["doc_type"] not in doc_types:
            return False
        # 표현은 목표 등급 이하만, 등급 기준은 목표 등급 것만 통과시킨다.
        if max_rank is not None:
            if meta["doc_type"] == "expression" and LEVEL_ORDER.index(meta["min_level"]) > max_rank:
                return False
            if meta["doc_type"] == "level" and meta["level"] != level:
                return False
        if question_type and "question_types" in meta:
            types = meta["question_types"]
            if question_type not in types and "전체" not in types:
                return False
        return True

    return _filter


def retrieve(
    query: str,
    k: int = 5,
    doc_types: Optional[list[str]] = None,
    level: Optional[str] = None,
    question_type: Optional[str] = None,
    store: Optional[InMemoryVectorStore] = None,
) -> list[Document]:
    """질문과 비슷한 참고 문서를 찾아 돌려준다.

    Args:
        query: 검색할 내용. 예: "공원 묘사할 때 쓸 표현", "비 와서 계획이 틀어진 경험".
        k: 돌려줄 문서 수.
        doc_types: 찾을 문서 종류. topic, roleplay, level, question_type_guide, expression 중에서 고른다.
            비워 두면 모든 종류에서 찾는다.
        level: 목표 등급(IM1~AL). 주면 그 등급 이하의 표현과 그 등급의 기준만 찾는다.
        question_type: 문제 유형. 주면 그 유형(또는 모든 유형)에 해당하는 문서만 찾는다.
        store: 사용할 벡터 저장소. 비워 두면 Bedrock 임베딩 저장소를 사용한다.

    Returns:
        유사도 점수(metadata["score"])가 붙은 문서 목록. 점수가 높은 순서.
    """
    if not query or not query.strip():
        return []
    level = level.strip().upper() if level else None
    store = store or get_vector_store()
    results = store.similarity_search_with_score(
        query,
        k=k,
        filter=_make_filter(doc_types, level, question_type),
    )
    docs = []
    for doc, score in results:
        doc.metadata = {**doc.metadata, "score": round(float(score), 4)}
        docs.append(doc)
    return docs


def to_contexts(docs: list[Document]) -> list[dict]:
    """검색 결과를 응답의 contexts 키에 넣을 형식으로 바꾼다."""
    return [
        {
            "source": d.metadata["source"],
            "doc_type": d.metadata["doc_type"],
            "id": d.metadata["id"],
            "score": d.metadata.get("score"),
            "content": d.page_content,
        }
        for d in docs
    ]
