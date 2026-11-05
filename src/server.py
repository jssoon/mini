"""OPIC 스크립트 생성 에이전트 API 서버.

POST /query 로 {"question": "..."} 를 받아 {"answer", "contexts", "trace"} 를 돌려준다.

실행 (프로젝트 루트에서)
    uvicorn src.server:app --host 0.0.0.0 --port 8000
"""

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src import agent

logger = logging.getLogger(__name__)

app = FastAPI(title="OPIC 스크립트 생성 Agent")


class QueryRequest(BaseModel):
    """POST /query 요청 본문."""

    question: str = Field(..., description="사용자 요청 문장", examples=["공원 묘사 스크립트 IH로 만들어줘"])


class QueryResponse(BaseModel):
    """POST /query 응답 본문 (제출 규약: answer, contexts, trace)."""

    answer: str
    contexts: list[dict[str, Any]]
    trace: list[dict[str, Any]]


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest):
    """질문을 에이전트에 넘겨 스크립트를 만들고, answer·contexts·trace를 돌려준다.

    에이전트 실행 중 오류(Bedrock 연결 실패 등)가 나도 같은 세 키 형식으로 500 응답을 돌려준다.
    """
    try:
        return agent.run(request.question)
    except Exception as e:
        logger.exception("에이전트 실행 실패")
        return JSONResponse(
            status_code=500,
            content={
                "answer": "죄송합니다. 스크립트를 만드는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                "contexts": [],
                "trace": [{"step": "error", "type": type(e).__name__}],
            },
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
