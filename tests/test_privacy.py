"""외부로 보내는 프로필 문장의 연락처 형식 검사 (이슈 #31, 정책 eligibility.md 16장). 네트워크를 쓰지 않는다."""
import copy
import json
import re

import pytest

from agents.application_writer import write_application
from agents.profile_analyzer import find_evidence, profile_payload
from agents.task_linker import link_tasks
from core.llm_client import FakeClient
from core.privacy import contact_findings, reject_contact_info
from core.schemas import JobRequirement
from core.selection import select_job
from core.writer_input import build_writer_input
from pipeline_helpers import PROFILE, run
from tools import eval_eligibility as ev
from tools import run_demo as demo

EMAIL, PHONE, RRN, LINK = "hong.gildong@example.com", "010-1234-5678", "900101-1234567", "https://github.com/hong"
SECRETS = (EMAIL, PHONE, RRN, LINK, "1234-5678", "1234567", "hong.gildong")


def profile_with(**change):
    """가상 프로필 fixture의 복사본에서 문장 칸 하나를 바꾼다. change: 'skills' 같은 칸 이름 -> 새 값 (목록이면 끝에 더한다)."""
    data = ev._read_json(ev.SAMPLE_FIXTURE)["profile"]
    data = copy.deepcopy(data)
    for key, value in change.items():
        current = data.get(key, [])
        data[key] = [*current, value] if not isinstance(value, list) else value
    from core.schemas import EligibilityProfile
    return EligibilityProfile(**{**data, "as_of_month": ev._read_json(ev.SAMPLE_FIXTURE)["as_of_month"]})


def experience_with(text, field="actions"):
    base = copy.deepcopy(ev._read_json(ev.SAMPLE_FIXTURE)["profile"]["experiences"][0])
    base["id"] = "EXP-PRIVACY"
    base[field] = [text] if isinstance(base[field], list) else text
    return base


def external(fake):
    fake.external = True
    return fake


# ---------------------------------------------------------------- 찾는 것과 찾지 않는 것

@pytest.mark.parametrize("text,kind", [
    ("문의 hong.gildong@example.com", "이메일"), ("연락처 010-1234-5678", "전화번호"), ("전화 02-123-4567", "전화번호"),
    ("01012345678", "전화번호"), ("900101-1234567", "주민등록번호"), ("https://github.com/hong", "링크"), ("www.example.com", "링크"),
])
def test_each_contact_format_is_found_with_its_kind_and_position(text, kind):
    assert contact_findings({"experiences": [{"actions": [text]}]}) == [("experiences[0].actions[0]", kind)]


@pytest.mark.parametrize("text", ["기간 2026-11", "불만족 사례 200건 분류", "3년 이상 (36개월)", "2024.03 ~ 2025.02", "정확도 98.5% 달성", "Python, SQL, Figma", "인도 개발진과 협업"])
def test_ordinary_sentences_with_numbers_are_not_flagged(text):
    assert contact_findings(text) == []


def test_the_committed_virtual_profile_has_no_contact_format_in_what_is_sent():
    assert contact_findings(profile_payload(PROFILE)) == []


def test_the_error_names_the_position_and_kind_but_never_the_content():
    with pytest.raises(PermissionError) as e:
        reject_contact_info({"skills": [f"메일 {EMAIL}"], "experiences": [{"actions": [f"{PHONE} {LINK}"]}]})
    message = str(e.value)
    assert "skills[0](이메일)" in message and "experiences[0].actions[0](전화번호)" in message and "(링크)" in message
    assert not any(secret in message for secret in SECRETS)


def test_more_than_five_findings_are_summarized():
    with pytest.raises(PermissionError, match="외 3곳"):
        reject_contact_info({"skills": [EMAIL] * 8})


# ---------------------------------------------------------------- 근거 찾기: 외부 클라이언트로 보내기 전에 멈춘다

REQ = [JobRequirement(id="R1", level="required", type="skill", text="Git 협업")]


@pytest.mark.parametrize("field,value", [
    ("skills", f"SQL {EMAIL}"), ("certificates", f"자격증 {PHONE}"), ("other_qualifications", f"포트폴리오 {LINK}"),
])
def test_find_evidence_refuses_contact_format_in_sentence_fields_before_any_call(field, value):
    fake = external(FakeClient([]))
    with pytest.raises(PermissionError) as e:
        find_evidence(REQ, profile_with(**{field: value}), fake, allow_external_profile=True)
    assert fake.requests == [] and field in str(e.value) and not any(secret in str(e.value) for secret in SECRETS)


def test_find_evidence_refuses_contact_format_inside_an_experience_before_any_call():
    prof = profile_with(experiences=[*ev._read_json(ev.SAMPLE_FIXTURE)["profile"]["experiences"], experience_with(f"고객 문의 대응 {EMAIL}")])
    fake = external(FakeClient([]))
    with pytest.raises(PermissionError, match=r"experiences\[5\]\.actions\[0\]\(이메일\)"):
        find_evidence(REQ, prof, fake, allow_external_profile=True)
    assert fake.requests == []


def test_a_new_unknown_field_with_an_email_is_dropped_and_never_sent():
    data = copy.deepcopy(ev._read_json(ev.SAMPLE_FIXTURE)["profile"])
    data["experiences"][0]["memo"] = EMAIL
    data["email"] = EMAIL
    from core.schemas import EligibilityProfile
    prof = EligibilityProfile(**{**data, "as_of_month": "2026-11"})
    assert EMAIL not in json.dumps(profile_payload(prof), ensure_ascii=False)
    fake = external(FakeClient([{"judgements": [{"requirement_id": "R1", "status": "needs_check", "reason_code": "no_profile_info",
                                                 "question_for_user": "Git으로 협업해 본 적이 있나요?"}]}]))
    find_evidence(REQ, prof, fake, allow_external_profile=True)  # 검사를 통과하고 호출한다
    assert len(fake.requests) == 1 and EMAIL not in fake.requests[0].prompt


def test_an_internal_mock_client_is_not_checked_because_nothing_leaves():
    fake = FakeClient([{"judgements": [{"requirement_id": "R1", "status": "needs_check", "reason_code": "no_profile_info",
                                        "question_for_user": "Git으로 협업해 본 적이 있나요?"}]}])
    assert fake.external is False
    find_evidence(REQ, profile_with(skills=f"SQL {EMAIL}"), fake)
    assert len(fake.requests) == 1


# ---------------------------------------------------------------- 연결 후보, 초안

TASK_LINKS = json.loads(demo.TASK_LINKS_JSON.read_text(encoding="utf-8"))["sample-3"]


def test_link_tasks_refuses_contact_format_before_any_call():
    prof = profile_with(experiences=[experience_with(f"업무 {PHONE}", "actions"), *ev._read_json(ev.SAMPLE_FIXTURE)["profile"]["experiences"]])
    fake = external(FakeClient([{"links": TASK_LINKS["links"]}]))
    with pytest.raises(PermissionError) as e:
        link_tasks(prof, TASK_LINKS["tasks"], fake, allow_external_profile=True, is_synthetic=True)
    assert fake.requests == [] and "(전화번호)" in str(e.value) and PHONE not in str(e.value)
    ok = external(FakeClient([{"links": TASK_LINKS["links"]}]))
    link_tasks(PROFILE, TASK_LINKS["tasks"], ok, allow_external_profile=True, is_synthetic=True)  # 깨끗한 프로필은 통과한다
    assert len(ok.requests) == 1


def test_write_application_refuses_contact_format_in_the_facts_before_any_call():
    RUN = run()
    wi = build_writer_input(select_job(RUN, "sample-3", include_profile_evidence=True), PROFILE)
    fact = wi.usable_facts[0]
    bad = wi.model_copy(update={"usable_facts": [fact.model_copy(update={"text": f"{fact.text} {LINK}"}), *wi.usable_facts[1:]]})
    fake = external(FakeClient([]))
    with pytest.raises(PermissionError, match=r"초안 입력에 연락처 형식"):
        write_application(bad, fake, "cover_letter", allow_external_profile=True)
    assert fake.requests == []
    with pytest.raises(PermissionError) as e:
        write_application(bad, fake, "cover_letter", allow_external_profile=True)
    assert LINK not in str(e.value)


def test_write_application_checks_the_quotes_too():
    RUN = run()
    wi = build_writer_input(select_job(RUN, "sample-3", include_profile_evidence=True), PROFILE)
    fact = next(f for f in wi.usable_facts if f.evidence)
    evidence = [fact.evidence[0].model_copy(update={"quote": f"{fact.evidence[0].quote} {EMAIL}"}), *fact.evidence[1:]]
    bad = wi.model_copy(update={"usable_facts": [fact.model_copy(update={"evidence": evidence}), *[f for f in wi.usable_facts if f is not fact]]})
    fake = external(FakeClient([]))
    with pytest.raises(PermissionError, match=r"\(이메일\)"):
        write_application(bad, fake, "cover_letter", allow_external_profile=True)
    assert fake.requests == []


def test_the_message_shape_is_stable():
    with pytest.raises(PermissionError, match=re.compile(r"^프로필에 연락처 형식이 있어 외부 LLM으로 보내지 않는다: .+\(정책 eligibility\.md 16장\)")):
        reject_contact_info({"skills": [EMAIL]})
