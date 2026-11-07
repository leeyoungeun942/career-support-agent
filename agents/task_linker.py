"""주요 업무 연결 후보 (docs/policies/application-writing.md 2장, 결정 D14, 이슈 #23).

공고의 주요 업무 항목마다 "이 경험을 이 업무와 연결해 설명할 수 있다"는 후보를 만든다. AI가 업무별로 프로필 경험과 인용을 고르고,
코드가 인용이 프로필 원문에 있는지 다시 대조한다. 어긋나는 후보는 버리고 다시 묻지 않는다.

연결 후보는 사실(usable_facts)이 아니다. 코드는 인용이 프로필에 있다는 것까지만 보장하고, 그 경험이 정말 그 업무와 관련 있는지는 보장하지 않는다.
프로필 원문이 들어 있어 결과를 저장하지 않는다(사람이 확정한 가상 프로필 fixture만 예외).
"""
import json

from pydantic import BaseModel

from agents.profile_analyzer import profile_payload
from core.llm_client import LLMClient, LLMRequest, LLMStageError
from core.prompts import fill, load_prompt
from core.rules_ai import evidence_problem
from core.schemas import EligibilityProfile, ProfileEvidence
from core.writer_input import EvidenceRef, TaskLink, experience_details

STAGE = "link_tasks"
TASK_PREFIX = "T"


class LinkQuote(BaseModel):
    experience_id: str
    quote: str


class LinkOut(BaseModel):
    task_index: int
    quotes: list[LinkQuote] = []


class LinkResponse(BaseModel):
    links: list[LinkOut]


def build_links(profile: EligibilityProfile, tasks: list[str], raw: dict, labels: list[str] | None = None) -> list[TaskLink]:
    """모델 응답(또는 사람이 확정한 fixture)을 연결 후보로 바꾼다. 인용은 프로필 원문과 다시 대조하고 어긋나면 그 인용을 버린다.

    task_id는 업무 항목의 순서(T1…)를 그대로 쓴다. 인용이 하나도 남지 않은 업무는 후보를 만들지 않는다.
    없는 업무 번호나 같은 번호가 둘인 응답은 LLMStageError다.
    """
    if labels is not None and len(labels) != len(tasks):
        raise ValueError("업무 이름(labels)의 개수가 업무와 다르다")
    seen: set[int] = set()
    links: list[TaskLink] = []
    for item in raw["links"]:
        index = item["task_index"]
        if not 1 <= index <= len(tasks):
            raise LLMStageError(f"{STAGE}: 없는 업무 번호")
        if index in seen:
            raise LLMStageError(f"{STAGE}: 같은 업무의 후보가 둘")
        seen.add(index)
        good = []
        for q in item.get("quotes", []):
            ev = ProfileEvidence(experience_id=q["experience_id"], quote=q["quote"])
            if evidence_problem(profile, ev) is None:
                good.append(ev)
        if good:
            links.append(TaskLink(task_id=f"{TASK_PREFIX}{index}", text=tasks[index - 1], label=labels[index - 1] if labels else None,
                                  evidence=[EvidenceRef(experience_id=e.experience_id, quote=e.quote) for e in good],
                                  evidence_detail=experience_details(good, profile)))
    return sorted(links, key=lambda t: int(t.task_id[len(TASK_PREFIX):]))


def link_tasks(profile: EligibilityProfile, tasks: list[str], client: LLMClient, allow_external_profile: bool = False,
               is_synthetic: bool = False, job_id: str = "") -> list[TaskLink]:
    """주요 업무마다 프로필 경험을 이어 연결 후보를 만든다 (호출 1회).

    외부로 나가는 클라이언트에는 allow_external_profile=True와 가상 프로필이 필요하다. 아니면 PermissionError (정책 5장).
    통신 실패, 스키마 위반은 LLMStageError다.
    """
    external = getattr(client, "external", True)
    if external and not allow_external_profile:
        raise PermissionError("외부 LLM으로 프로필을 보내려면 allow_external_profile=True를 명시해야 한다 (정책 5장)")
    if external and not is_synthetic:
        raise PermissionError("가상이 아닌 프로필은 외부 LLM으로 보내지 않는다 (정책 5장)")
    if not tasks:
        return []
    version, body = load_prompt(STAGE)
    experiences = [{"experience_id": x["id"], **{k: v for k, v in x.items() if k in ("title", "role", "actions", "tools", "result")}}
                   for x in profile_payload(profile)["experiences"]]
    prompt = fill(
        body,
        tasks=json.dumps([{"task_index": i + 1, "text": t} for i, t in enumerate(tasks)], ensure_ascii=False, indent=1),
        experiences=json.dumps(experiences, ensure_ascii=False, indent=1),
    )
    request = LLMRequest(stage=STAGE, prompt=prompt, schema=LinkResponse, job_id=job_id, prompt_version=version, contains_profile=True)
    return build_links(profile, tasks, client.complete_json(request).data)
