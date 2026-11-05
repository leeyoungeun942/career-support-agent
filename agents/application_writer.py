"""자소서·포트폴리오 초안 Agent (docs/policies/application-writing.md, 이슈 #14).

LLM은 섹션별 문장(text)과 쓴 사실 id(fact_ids)만 낸다. 어떤 사실을 써도 되는지는 WriterInput(코드가 분류)이 정하고,
확인 필요 목록과 경고 문구는 코드가 입력에서 복사한다. 돌아온 초안은 validate_draft가 검증하고, 어기면 DraftError로 단계가 실패한다.
"""
import json
import re

from pydantic import BaseModel, Field

from core.llm_client import LLMClient, LLMRequest, MockClient
from core.prompts import fill, load_prompt
from core.writer_input import ToConfirm, WriterInput

COVER_LETTER, PORTFOLIO = "cover_letter", "portfolio"
KINDS = (COVER_LETTER, PORTFOLIO)
STAGE = {COVER_LETTER: "write_cover_letter", PORTFOLIO: "write_portfolio"}
SECTIONS: dict[str, dict[str, str]] = {
    COVER_LETTER: {
        "motivation": "지원동기 및 입사 후 포부",
        "job_experience": "직무 관련 경험과 역량",
        "quality": "꼼꼼함·품질 관리 경험",
        "collaboration": "커뮤니케이션·주도적 업무 경험",
    },
    PORTFOLIO: {
        "project_summary": "프로젝트·경험 제목과 한 줄 요약",
        "role_actions": "목표, 본인의 역할과 행동",
        "tools_results": "사용한 도구·방식, 결과",
        "job_connection": "지원 공고와 연결되는 이유",
        "highlight_experiences": "포트폴리오에서 강조하면 좋은 경험",
    },
}
DATA_MARKER = "\n## 입력 데이터\n"
DRAFT_BANNER = "초안입니다. 제출 전에 사실을 직접 확인하세요."

# 본문에서 확인하는 토큰: 숫자 덩어리와 영문 고유명(도구명, 자격증명 등). 정책 3장
_NUMBER = re.compile(r"(?<![A-Za-z\d])\d+(?:[.,]\d+)*(?![A-Za-z])")  # B2B 같은 영문 이름 안의 숫자는 영문 토큰이 맡는다
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[./-][A-Za-z0-9+#]+)*")


class DraftError(Exception):
    """초안이 정책 3장의 검증을 어겼다. code로 범주를 가른다. 메시지에는 초안 본문을 넣지 않는다."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class DraftSection(BaseModel):
    section: str
    text: str = Field(min_length=1)
    fact_ids: list[str] = []


class DraftResponse(BaseModel):
    """모델이 내는 응답. 확인 필요 목록과 경고는 모델이 쓰지 않는다."""

    sections: list[DraftSection] = Field(min_length=1)


class DraftMeta(BaseModel):
    mode: str  # mock / ai
    model: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0


class ApplicationDraft(BaseModel):
    kind: str
    job_id: str
    run_id: str
    profile_id: str
    is_synthetic: bool
    sections: list[DraftSection]
    to_confirm: list[ToConfirm]
    notices: list[str]
    banner: str = DRAFT_BANNER
    meta: DraftMeta


_ID_KEYS = {"id", "experience_id", "fact_id", "requirement_id", "confirm_id", "forbidden_id", "item_index", "job_id", "level"}


def _texts(obj) -> list[str]:
    """값 안의 글만 모은다. id 같은 칸(exp-2, R2)은 근거가 되는 글이 아니라서 뺀다."""
    if isinstance(obj, dict):
        return [t for k, v in obj.items() if k not in _ID_KEYS for t in _texts(v)]
    if isinstance(obj, list):
        return [t for v in obj for t in _texts(v)]
    return [str(obj)] if obj is not None else []


def _corpus(wi: WriterInput) -> str:
    """본문의 숫자·영문 토큰이 근거로 삼을 수 있는 글: 프로필, 공고 내용, 사실 문장."""
    parts = [*_texts(wi.profile), *_texts(wi.job.model_dump()), *_texts([f.model_dump() for f in wi.usable_facts])]
    return "\n".join(parts).casefold()


def validate_draft(response: dict, wi: WriterInput, kind: str) -> list[DraftSection]:
    """모델 응답(DraftResponse 모양)을 정책 3장대로 검증한다. 어기면 DraftError. 통과한 섹션 목록을 돌려준다."""
    allowed = SECTIONS[kind]
    sections = [DraftSection(**s) for s in response["sections"]]
    seen: set[str] = set()
    corpus = _corpus(wi)
    facts, forbidden = wi.fact_ids, wi.forbidden_ids
    for s in sections:
        if s.section not in allowed or s.section in seen:
            raise DraftError("bad_section", s.section if s.section not in allowed else f"{s.section} 중복")
        seen.add(s.section)
        if not s.fact_ids:
            raise DraftError("missing_fact_id", s.section)
        for fid in s.fact_ids:
            if fid in forbidden:
                raise DraftError("forbidden_fact_id", f"{s.section}: {fid}")
            if fid not in facts:
                raise DraftError("unknown_fact_id", f"{s.section}: {fid}")
        for token in _NUMBER.findall(s.text):
            if not re.search(rf"(?<![A-Za-z\d.,]){re.escape(token)}(?![A-Za-z]|[.,]?\d)", corpus):  # 다른 숫자나 영문 이름(B2B 등)의 일부가 아닌 같은 숫자가 입력에 있어야 한다
                raise DraftError("unsupported_token", f"{s.section}: {token}")
        for token in _LATIN.findall(s.text):
            if token.casefold() not in corpus:
                raise DraftError("unsupported_token", f"{s.section}: {token}")
    return sections


def _split(body: str) -> tuple[str, str]:
    head, sep, tail = body.partition(DATA_MARKER)
    if not sep:
        raise ValueError(f"프롬프트에 {DATA_MARKER.strip()!r} 구분이 없다")
    return head, DATA_MARKER.lstrip("\n") + tail


def _data(wi: WriterInput) -> dict[str, str]:
    j = lambda obj: json.dumps(obj, ensure_ascii=False, indent=1)  # noqa: E731
    return {
        "job": j(wi.job.model_dump(exclude={"job_label"})),
        "profile": j(wi.profile),
        "usable_facts": j([f.model_dump(exclude_none=True) for f in wi.usable_facts]),
        "to_confirm": j([{"confirm_id": c.confirm_id, "text": c.text} for c in wi.to_confirm]),
        "do_not_claim": j([{"forbidden_id": x.forbidden_id, "text": x.text} for x in wi.do_not_claim]),
    }


def write_application(wi: WriterInput, client: LLMClient, kind: str, allow_external_profile: bool = False,
                      mode: str = "mock") -> ApplicationDraft:
    """초안 1건(자소서 또는 포트폴리오)을 만든다.

    외부 클라이언트에는 allow_external_profile=True를 명시해야 프로필을 보내고, 가상이 아닌 프로필은 외부로 보내지 않는다 (정책 5장).
    통신·스키마 실패는 LLMStageError, 정책 3장의 검증 실패는 DraftError다. 다시 묻지 않는다.
    """
    if kind not in KINDS:
        raise ValueError(f"kind는 {KINDS} 중 하나여야 한다")
    external = getattr(client, "external", True)
    if external and not allow_external_profile:
        raise PermissionError("외부 LLM으로 프로필을 보내려면 allow_external_profile=True를 명시해야 한다 (정책 5장)")
    if external and not wi.meta.is_synthetic:
        raise PermissionError("가상이 아닌 프로필은 외부 LLM으로 보내지 않는다 (정책 5장)")
    if not wi.usable_facts:
        raise DraftError("no_usable_facts", "쓸 수 있는 사실이 없다")
    version, body = load_prompt(STAGE[kind])
    system_frame, data_frame = _split(body)
    sections_text = json.dumps(SECTIONS[kind], ensure_ascii=False, indent=1)
    request = LLMRequest(stage=STAGE[kind], prompt=fill(data_frame, **_data(wi)), system=fill(system_frame, sections=sections_text),
                         schema=DraftResponse, job_id=wi.meta.job_id, prompt_version=version, contains_profile=True)
    response = client.complete_json(request)
    sections = validate_draft(response.data, wi, kind)
    return ApplicationDraft(
        kind=kind, job_id=wi.meta.job_id, run_id=wi.meta.run_id, profile_id=wi.meta.profile_id, is_synthetic=wi.meta.is_synthetic,
        sections=sections, to_confirm=list(wi.to_confirm), notices=list(wi.notices),
        meta=DraftMeta(mode=mode, model=response.model, prompt_version=version,
                       input_tokens=response.input_tokens, output_tokens=response.output_tokens),
    )


def make_writer_mock(answers: dict) -> MockClient:
    """answers: {job_id: {kind: 응답 dict}}. 네트워크를 쓰지 않는 mock 클라이언트를 만든다 (fixture의 초안을 돌려준다)."""
    return MockClient({(STAGE[kind], job_id): resp for job_id, by_kind in answers.items() for kind, resp in by_kind.items()})
