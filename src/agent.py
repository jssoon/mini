"""OPIC 스크립트 생성 메인 에이전트 그래프.

흐름: 질문 → guard(요청 분류) → 정상 요청이면 react(ReAct 에이전트), 아니면 refuse(정중한 거절)
run()은 제출 규약에 맞춰 answer, contexts, trace 세 키를 가진 딕셔너리를 돌려준다.

환경 변수 (.env)
    AWS_REGION              Bedrock 리전 (없으면 AWS_DEFAULT_REGION, 그것도 없으면 us-east-1)
    BEDROCK_MODEL_ID        채팅 모델 ID (기본값: anthropic.claude-opus-5-5)
    BEDROCK_GUARD_MODEL_ID  요청 분류용 모델 ID (없으면 BEDROCK_MODEL_ID와 같은 모델)
"""

import json
import os
from typing import Any, Optional, TypedDict

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from src.tools import TOOLS

load_dotenv()

DEFAULT_MODEL_ID = "anthropic.claude-opus-5-5"
DEFAULT_REGION = "us-east-1"
RECURSION_LIMIT = 20  # 도구 호출 1회 = 2단계. 대략 도구 9회 안에서 끝나도록 제한한다.
TRACE_OUTPUT_LIMIT = 500  # trace에 남길 도구 결과 최대 글자 수

# contexts에 근거 자료로 남길 조회 도구 (check_script는 검사 결과라 제외)
CONTEXT_TOOLS = {
    "list_topics", "get_topic_info", "get_level_criteria",
    "search_expressions", "get_roleplay_scenarios",
}

# ---------------------------------------------------------------------------
# 프롬프트
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """너는 OPIC(영어 말하기 시험) 연습 스크립트를 만들어 주는 코치다.
시험생의 주제, 문제 유형, 목표 등급, 개인 경험 키워드에 맞춘 스크립트를 만든다.

## 작업 순서
1. 요청에서 주제, 문제 유형(묘사/루틴/과거 경험/비교/롤플레이), 목표 등급(IM1~AL), 개인 경험 키워드를 파악한다.
   - 등급이 없으면 IM3, 유형이 없으면 주제에 가장 자연스러운 유형을 쓴다.
     주제만 알 수 있으면 되묻지 말고 기본값으로 바로 만든다.
   - 주제를 전혀 알 수 없을 만큼 요청이 짧으면 도구를 쓰지 말고 주제·유형·등급을 되묻는다.
2. 자료 조회는 첫 턴에 한 번에 함께 호출하고, 같은 도구를 다시 호출하지 않는다.
   get_level_criteria(등급 기준·유형 가이드), get_topic_info(주제 질문·어휘),
   search_expressions(등급에 맞는 표현)는 서로 결과를 기다릴 필요가 없으므로 같은 턴에 동시에 호출한다.
   롤플레이라면 get_roleplay_scenarios도 함께 호출한다.
   get_topic_info가 후보를 여러 개 돌려주면 그중 요청에 맞는 것을 골라 바로 쓴다.
   search_references는 get_topic_info가 주제를 찾지 못했을 때만 쓴다.
3. 스크립트를 쓰기 전에 등급 기준의 문장 수와 단어 수 범위에서 각각 가운데 값을 목표로 정하고, 문장을 하나씩 세면서 쓴다.
   (예: IM3는 8~10문장·110~160단어이므로 9문장·약 135단어) 한 문장은 평균 단어 수(목표 단어 ÷ 목표 문장)에 맞춘다.
4. 다 쓴 스크립트는 보여 주기 전에 check_script로 검사한다.
   keywords에는 사용자가 준 개인 경험 키워드만, 스크립트에 실제로 쓴 영어 표현으로 넘긴다.
   사용자가 키워드를 주지 않았으면 keywords를 넘기지 않는다. 스스로 정한 표현이나 핵심 표현은 넘기지 않는다.
   통과하지 못하면 문제가 된 부분만 고쳐서 다시 검사하되 재검사는 2회까지만 한다.
5. 콤보 세트 요청이면 같은 주제로 묘사 → 과거 경험 → 비교 3개 스크립트를 만든다.
   - get_level_criteria는 question_type="묘사, 과거 경험, 비교"로 한 번만 호출한다.
   - 3개를 모두 쓴 뒤 check_script 3번을 같은 턴에 동시에 호출하고, 통과하지 못한 것만 고쳐서 다시 검사한다.
   - 사용자 키워드는 3개 스크립트에 나눠 넣고, 각 검사에는 그 스크립트에 넣은 키워드만 넘긴다.

## 답변 형식 (마크다운)
- 답변은 아래 형식으로 바로 시작한다. 검사 결과, 도구 이름, 문장 수·단어 수 같은 작업 과정은 답변에 쓰지 않는다.
  재검사 후에도 "검사 통과", "완성됐어요" 같은 말 없이 기본값 안내 줄(있으면) 또는 "### 질문"으로 시작한다.
- 기본값을 썼다면 맨 첫 줄에 한 줄로 밝힌다. (예: "> 목표 등급을 정하지 않아 IM3 기준으로 만들었습니다.")

### 질문
(이 스크립트가 답하는 영어 질문)
### 스크립트
(영어 스크립트)
### 한국어 해석
### 핵심 표현
(3~5개, 표현 - 뜻)
사용자가 암기용 키워드 카드를 요청하면 "### 키워드 카드"(핵심어 5~7개)를 덧붙인다.

## 작성 원칙
- 외운 티가 나지 않도록 실제 말하기처럼 구어체와 필러를 자연스럽게 섞는다.
- 문법적으로 정확한 영어를 쓴다. 교재·유료 강의 스크립트를 그대로 옮기지 않고 새로 쓴다.
- 혐오·폭력·선정적 내용, 특정 인물·집단을 비하하는 표현은 쓰지 않는다.
- 사용자가 실명·전화번호·주소·이메일을 알려 줘도 스크립트와 해석에는 하나도 넣지 않는다.
  이름은 이름 일부나 별명도 쓰지 않고 "I'm an office worker in my thirties"처럼 이름 없이 소개한다.
  주소는 "a quiet neighborhood in Seoul"처럼 구·동 이하를 빼고 쓴다.
  이런 정보를 받았다면 답변 끝에 "민감한 개인정보(이름, 연락처, 주소)는 스크립트에서 뺐습니다. 다음에는 입력하지 않는 것이 좋습니다"라고 안내한다.
- 점수나 등급을 묻거나 보장해 달라고 하면, 등급 기준은 참고용이며 실제 점수를 보장하지 않는다고 분명히 말한다.
"""

GUARD_PROMPT = """너는 OPIC 스크립트 생성 서비스의 요청 분류기다. 사용자 요청을 아래 라벨 중 하나로 분류한다.
이 서비스로 들어오는 요청은 대부분 OPIC 연습용이다. "OPIC"이나 "스크립트"라는 말이 없어도
집·동네·직업·취미·운동·여행·일상 경험 등을 묘사하거나 이야기해 달라는 요청은 연습 스크립트 요청이므로 OPIC이다.
"스크립트"처럼 너무 짧거나 애매한 요청도 OPIC으로 분류한다. 아래 거절 라벨에 확실히 해당할 때만 거절 라벨을 쓴다.

- OPIC: OPIC·영어 말하기 연습 스크립트 요청, 일상 주제 묘사·경험 이야기 요청, 또는 OPIC 준비·등급·답변 방법에 대한 질문
  (개인정보가 섞여 있어도 OPIC) 예: "내가 사는 아파트 묘사해줘", "주말에 하는 일 말해줘 IH", "스크립트"
- UNRELATED: OPIC·영어 말하기 연습과 관계없는 요청 (코딩, 메뉴 추천, 토익 문법 풀이 등)
- CHEATING: 시험 중 실시간 답변, 유출 문제·답 제공, 시험장에서 몰래 보기 등 부정행위 목적
- COPYRIGHT: 특정 교재·유료 강의의 스크립트 원문을 그대로 옮겨 달라는 요청
- HARMFUL: 혐오, 특정 국가·집단 비하, 폭력적·선정적 묘사를 담아 달라는 요청

설명 없이 라벨 한 단어만 출력한다."""

GUARD_LABELS = ["OPIC", "UNRELATED", "CHEATING", "COPYRIGHT", "HARMFUL"]

REFUSAL_MESSAGES = {
    "UNRELATED": (
        "죄송하지만 이 서비스는 OPIC 영어 말하기 연습 스크립트만 만들어 드립니다. "
        "연습하고 싶은 주제와 목표 등급을 알려 주시면 맞춤 스크립트를 만들어 드릴게요. "
        "(예: \"카페 가기 루틴 IM3로 만들어줘\")"
    ),
    "CHEATING": (
        "시험 중 답변 제공이나 유출 문제 제공 같은 부정행위는 도와드릴 수 없습니다. "
        "대신 시험 전에 연습할 수 있도록 원하는 주제의 연습 스크립트를 만들어 드릴 수 있어요."
    ),
    "COPYRIGHT": (
        "교재나 유료 강의의 스크립트 원문은 저작권 때문에 그대로 옮겨 드릴 수 없습니다. "
        "같은 주제로 새 스크립트를 만들어 드릴까요? 주제와 목표 등급을 알려 주세요."
    ),
    "HARMFUL": (
        "특정 집단을 비하하거나 폭력적·선정적인 내용은 스크립트에 담을 수 없습니다. "
        "같은 주제를 중립적인 내용으로 바꿔 스크립트를 만들어 드릴 수 있어요."
    ),
}


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------

def get_model(model_id: Optional[str] = None, max_tokens: int = 8000) -> BaseChatModel:
    """Bedrock 채팅 모델(ChatBedrockConverse)을 만든다.

    Args:
        model_id: 모델 ID. 비워 두면 환경 변수 BEDROCK_MODEL_ID를 쓴다.
        max_tokens: 한 번에 생성할 최대 토큰 수.
    """
    from botocore.config import Config
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(
        model_id=model_id or os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_REGION,
        max_tokens=max_tokens,
        # 요청 한도 초과(ThrottlingException) 시 간격을 늘려 가며 자동으로 다시 시도한다.
        config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=120),
    )


def get_guard_model() -> BaseChatModel:
    """요청 분류용 모델을 만든다. BEDROCK_GUARD_MODEL_ID가 없으면 메인 모델을 쓴다."""
    return get_model(os.getenv("BEDROCK_GUARD_MODEL_ID"), max_tokens=1024)


# ---------------------------------------------------------------------------
# 그래프
# ---------------------------------------------------------------------------

class State(TypedDict, total=False):
    """그래프 노드 사이에서 주고받는 상태."""

    question: str
    label: str
    messages: list[BaseMessage]
    answer: str


def _text(message: BaseMessage) -> str:
    """메시지 본문에서 텍스트만 뽑는다. 추론(reasoning) 블록 등은 제외한다."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "".join(parts)


def _strip_preamble(answer: str) -> str:
    """스크립트 답변 앞에 붙은 작업 과정 설명("검사 통과!" 등)을 지운다.

    "### 스크립트"가 있는 답변에서만, 첫 번째 인용(>)이나 제목(#) 줄 앞의 내용을 지운다.
    """
    if "### 스크립트" not in answer:
        return answer
    lines = answer.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith((">", "#"))), 0)
    return "\n".join(lines[start:]).strip()


def _parse_label(text: str) -> str:
    """분류기 출력에서 라벨을 찾는다. 찾지 못하면 OPIC으로 보고 에이전트의 가드레일에 맡긴다."""
    upper = text.upper()
    for label in GUARD_LABELS:
        if label in upper:
            return label
    return "OPIC"


def build_graph(model: Optional[BaseChatModel] = None, guard_model: Optional[BaseChatModel] = None):
    """guard → (react | refuse) 구조의 에이전트 그래프를 만든다.

    model을 넘기지 않으면 Bedrock 모델을 사용한다. 테스트에서는 가짜 모델을 넘길 수 있다.
    guard_model을 넘기지 않으면, model을 넘긴 경우 같은 모델을, 아니면 분류용 Bedrock 모델을 쓴다.
    """
    if guard_model is None:
        guard_model = model or get_guard_model()
    model = model or get_model()
    react_agent = create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)

    def guard(state: State) -> State:
        """요청이 OPIC 연습 요청인지, 거절해야 할 요청인지 분류한다."""
        result = guard_model.invoke([
            SystemMessage(content=GUARD_PROMPT),
            HumanMessage(content=state["question"]),
        ])
        return {"label": _parse_label(_text(result))}

    def refuse(state: State) -> State:
        """거절 라벨에 맞는 안내 메시지로 답한다. 도구는 호출하지 않는다."""
        return {"answer": REFUSAL_MESSAGES[state["label"]], "messages": []}

    def react(state: State) -> State:
        """ReAct 에이전트가 도구를 호출하며 스크립트를 만든다."""
        try:
            result = react_agent.invoke(
                {"messages": [HumanMessage(content=state["question"])]},
                config={"recursion_limit": RECURSION_LIMIT},
            )
        except GraphRecursionError:
            return {
                "answer": "스크립트를 만드는 데 시간이 너무 오래 걸려 중단했습니다. 요청을 조금 더 구체적으로 다시 보내 주세요.",
                "messages": [],
            }
        messages = result["messages"]
        final = next((m for m in reversed(messages) if isinstance(m, AIMessage) and _text(m)), None)
        return {"answer": _strip_preamble(_text(final)) if final else "", "messages": messages}

    graph = StateGraph(State)
    graph.add_node("guard", guard)
    graph.add_node("refuse", refuse)
    graph.add_node("react", react)
    graph.add_edge(START, "guard")
    graph.add_conditional_edges(
        "guard",
        lambda s: "react" if s["label"] == "OPIC" else "refuse",
        {"react": "react", "refuse": "refuse"},
    )
    graph.add_edge("refuse", END)
    graph.add_edge("react", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# 응답 만들기 (answer, contexts, trace)
# ---------------------------------------------------------------------------

def _collect(label: str, messages: list[BaseMessage]) -> tuple[list[dict], list[dict]]:
    """에이전트 메시지 기록에서 contexts(근거 자료)와 trace(ReAct 단계)를 뽑는다."""
    contexts: list[dict] = []
    trace: list[dict] = [{"step": "guard", "label": label}]

    for m in messages:
        if isinstance(m, AIMessage):
            thought = _text(m)
            if m.tool_calls and thought:
                trace.append({"step": "thought", "content": thought})
            for call in m.tool_calls:
                trace.append({"step": "action", "tool": call["name"], "input": call["args"]})
        elif isinstance(m, ToolMessage):
            output = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
            trace.append({
                "step": "observation",
                "tool": m.name,
                "output": output[:TRACE_OUTPUT_LIMIT],
            })
            if m.name == "search_references":
                try:
                    contexts.extend(json.loads(output).get("contexts", []))
                except (json.JSONDecodeError, AttributeError):
                    pass
            elif m.name in CONTEXT_TOOLS:
                contexts.append({"source": m.name, "content": output})

    trace.append({"step": "answer"})
    return contexts, trace


_GRAPH = None


def run(question: str, graph=None) -> dict[str, Any]:
    """질문 하나를 처리해 answer, contexts, trace를 돌려준다.

    Args:
        question: 사용자 요청 문장.
        graph: 사용할 그래프. 비워 두면 Bedrock 모델 그래프를 한 번 만들어 재사용한다.
    """
    global _GRAPH
    if not question or not question.strip():
        return {
            "answer": "연습하고 싶은 주제와 목표 등급을 알려 주세요. (예: \"공원 묘사 IH로 만들어줘\")",
            "contexts": [],
            "trace": [{"step": "answer"}],
        }
    if graph is None:
        if _GRAPH is None:
            _GRAPH = build_graph()
        graph = _GRAPH

    state = graph.invoke({"question": question.strip()})
    contexts, trace = _collect(state["label"], state.get("messages", []))
    return {"answer": state["answer"], "contexts": contexts, "trace": trace}
