"""초안이 업무 연결 후보를 인용하는 규칙 테스트 (정책 application-writing.md 3·4장, 결정 D14~D16, 이슈 #23). 네트워크를 쓰지 않는다."""
import json
from pathlib import Path

import pytest
from pipeline_helpers import PROFILE, run

from agents import application_writer as aw
from agents.application_writer import DraftError, write_application
from agents.task_linker import build_links
from core.llm_client import FakeClient
from core.prompts import load_prompt
from core.selection import select_job
from core.writer_input import JobInfo, build_writer_input
from tools import run_demo as demo

RUN = run()
FIXTURE = json.loads(Path(demo.TASK_LINKS_JSON).read_text(encoding="utf-8"))["sample-3"]
TASKS = FIXTURE["tasks"]
LINKS = build_links(PROFILE, TASKS, {"links": FIXTURE["links"]})
POLICY = (Path(__file__).resolve().parents[1] / "docs" / "policies" / "application-writing.md").read_text(encoding="utf-8")


def wi_for(job_id="sample-3", links=True, main_tasks=()):
    sel = select_job(RUN, job_id, include_profile_evidence=True)
    return build_writer_input(sel, PROFILE, JobInfo(main_tasks=list(main_tasks)), task_links=LINKS if links else None)


def para(text, fact_ids=(), task_ids=(), section="job_experience", heading="소제목"):
    return {"sections": [{"section": section, "heading": heading,
                          "paragraphs": [{"text": text, "fact_ids": list(fact_ids), "task_ids": list(task_ids)}]}]}


def draft(resp, job_id="sample-3", kind="cover_letter", **kw):
    return write_application(wi_for(job_id, **kw), FakeClient([resp]), kind)


def code_of(resp, job_id="sample-3", kind="cover_letter", **kw):
    with pytest.raises(DraftError) as e:
        draft(resp, job_id, kind, **kw)
    return e.value.code


# ---------------------------------------------------------------- 근거 규칙: fact_ids 또는 task_ids 하나 이상 (D15)

def test_a_paragraph_needs_a_fact_or_a_task_and_either_alone_is_enough():
    assert code_of(para("경험이 있습니다.")) == "missing_evidence"
    assert draft(para("카페 안내 페이지를 만들었습니다.", fact_ids=["F3"]))
    assert draft(para("카페 안내 페이지를 만들었습니다.", task_ids=["T1"]))
    assert draft(para("카페 안내 페이지를 만들었습니다.", fact_ids=["F3"], task_ids=["T1"]))


def test_without_task_links_every_task_id_is_unknown_and_nothing_changes_for_fact_ids():
    assert code_of(para("경험이 있습니다.", task_ids=["T1"]), links=False) == "unknown_task_id"
    assert draft(para("경험이 있습니다.", fact_ids=["F1"]), links=False)


def test_unknown_task_ids_and_forbidden_ids_in_the_task_slot_are_rejected():
    assert code_of(para("경험이 있습니다.", task_ids=["T9"])) == "unknown_task_id"
    assert code_of(para("경험이 있습니다.", task_ids=["C1"])) == "forbidden_fact_id"  # 확인 필요 id를 연결 후보 자리로 우회
    assert code_of(para("경험이 있습니다.", task_ids=["X1"]), job_id="sample-1") == "forbidden_fact_id"  # 주장 금지 id도 마찬가지


def test_bullets_follow_the_same_rule():
    resp = para("문단입니다.", task_ids=["T1"], section="highlight_experiences")
    resp["sections"][0]["bullets"] = [{"text": "항목", "fact_ids": [], "task_ids": []}]
    assert code_of(resp, kind="portfolio") == "missing_evidence"
    resp["sections"][0]["bullets"] = [{"text": "항목", "fact_ids": [], "task_ids": ["T2"]}]
    assert draft(resp, kind="portfolio")


# ---------------------------------------------------------------- task_ids만 있는 문단은 충족·보유·수준을 주장하지 못한다 (D15, D16)

@pytest.mark.parametrize("text", [
    "서비스 기획 경력 요건을 충족합니다.",
    "필요한 역량을 갖추었습니다.",
    "이 업무를 수행할 수 있습니다.",
    "이 업무를 충분히 해낼 수 있습니다.",
    "누구보다 능숙합니다.",
    "이 분야에 숙달했습니다.",
    "전문가입니다.",
])
def test_task_only_paragraphs_cannot_claim_the_requirement_or_the_ability(text):
    assert code_of(para(text, task_ids=["T3"])) == "task_only_claim"


def test_the_same_words_are_not_blocked_when_a_usable_fact_backs_the_paragraph():
    assert draft(para("문서 정제 경험으로 요건을 충족합니다.", fact_ids=["F3"], task_ids=["T3"]))


def test_describing_a_related_experience_is_allowed_even_in_a_task_only_paragraph():
    for text in ("카페 안내 페이지의 정보 구조를 설계한 경험을 이 업무와 연결해 설명할 수 있습니다.",
                 "관련된 경험으로 문서 정제 작업을 보조했습니다."):
        assert draft(para(text, task_ids=["T1"]))


def test_an_unmet_requirement_cannot_be_claimed_through_a_task_link():
    # 샘플 1번은 경력 3년(R2)이 not_met이다. 연결 후보로 우회해도 숫자 근거와 금지 표현에서 막힌다
    assert code_of(para("서비스 기획 경력 3년 이상을 갖추었습니다.", task_ids=["T3"]), job_id="sample-1") == "task_only_claim"
    assert code_of(para("서비스 기획 경력 3년의 경험이 있습니다.", task_ids=["T3"]), job_id="sample-1") == "unsupported_token"
    assert draft(para("관련된 경험으로 시나리오와 가이드를 작성했습니다.", task_ids=["T3"]), job_id="sample-1")


# ---------------------------------------------------------------- 토큰 근거: 인용한 연결 후보의 근거만 (D6)

def test_tokens_come_only_from_the_cited_links_evidence():
    # T1 = exp-3 + exp-4(Notion), T2 = exp-3 + exp-1(Figma), T4 = exp-1(약 200건) + exp-5(ATS 없음: 행동에 ATS가 있는 것은 exp-5 행동 2번)
    assert draft(para("Notion 목차 페이지로 정리했습니다.", task_ids=["T1"]))
    assert code_of(para("Figma로 화면을 그렸습니다.", task_ids=["T1"])) == "unsupported_token"
    assert draft(para("Figma로 화면을 그렸습니다.", task_ids=["T2"]))
    assert code_of(para("약 200건을 분석했습니다.", task_ids=["T1"])) == "unsupported_token"
    assert draft(para("약 200건을 분석했습니다.", task_ids=["T4"]))
    assert code_of(para("ATS 기준을 설계했습니다.", task_ids=["T1"])) == "unsupported_token"  # 인용하지 않은 경험(exp-5)


def test_ids_the_task_text_and_the_periods_are_not_token_evidence():
    for text in ("exp-3 경험입니다.", "T1 경험입니다.", "2026.03에 했습니다.", "2026에 했습니다."):
        assert code_of(para(text, task_ids=["T1"])) == "unsupported_token", text


def test_team_names_from_the_task_text_only_in_the_connection_sections():
    wi = wi_for(main_tasks=TASKS)
    ok = write_application(wi, FakeClient([para("AX팀과의 조율에 이 경험을 연결해 설명하겠습니다.", task_ids=["T5"], section="motivation")]), "cover_letter")
    assert ok.sections[0].paragraphs[0].task_ids == ["T5"]
    with pytest.raises(DraftError) as e:
        write_application(wi, FakeClient([para("AX팀과의 조율에 이 경험을 연결해 설명하겠습니다.", task_ids=["T5"], section="collaboration")]), "cover_letter")
    assert e.value.code == "unsupported_token"  # 프롬프트 v3가 모델에게 알려 주지 않았던 규칙 (정책 8-2의 AX 거부)


def test_the_heading_may_use_what_the_section_cited_through_tasks():
    assert draft(para("글", task_ids=["T4"], heading="약 200건의 사례 분류"))
    assert code_of(para("글", task_ids=["T1"], heading="약 200건의 사례 분류")) == "unsupported_token"


# ---------------------------------------------------------------- 연결 후보는 사실이 아니다: 표시와 프롬프트

def test_the_draft_records_only_the_cited_tasks_without_profile_quotes():
    d = draft({"sections": [{"section": "job_experience", "heading": "제목", "paragraphs": [
        {"text": "정보 구조를 설계했습니다.", "task_ids": ["T1"]}, {"text": "구성안을 기획했습니다.", "fact_ids": ["F3"], "task_ids": ["T2", "T1"]}]}]})
    assert [t.task_id for t in d.linked_tasks] == ["T1", "T2"] and d.linked_tasks[0].text == TASKS[0]
    dumped = d.model_dump_json()
    assert "evidence" not in dumped and "카페 정보를 수집" not in dumped  # 프로필 인용·근거 경험은 초안에 남지 않는다


def test_request_gets_the_link_block_and_rules_only_when_links_exist():
    with_links, without = FakeClient([para("글", task_ids=["T1"])]), FakeClient([para("글", fact_ids=["F1"])])
    write_application(wi_for(), with_links, "cover_letter")
    write_application(wi_for(links=False), without, "cover_letter")
    a, b = with_links.requests[0], without.requests[0]
    assert a.prompt_version == load_prompt(aw.STAGE["cover_letter"])[0] + "+links" and b.prompt_version == load_prompt(aw.STAGE["cover_letter"])[0]
    assert "task_links" in a.prompt and "사실이 아니다" in a.prompt and "task_ids" in a.system and "판정이 아니다" in a.system
    assert "task_ids" not in b.prompt and "task_ids" not in (b.system or "") and "task_links" not in b.prompt  # 연결 후보가 없으면 기준선 프롬프트와 같다
    assert a.contains_profile is True and "{{" not in a.prompt and "{{" not in a.system


def test_screen_and_md_label_task_links_as_candidates_not_facts(tmp_path):
    d = draft({"sections": [{"section": "job_experience", "heading": "제목", "paragraphs": [
        {"text": "정보 구조를 설계했습니다.", "fact_ids": ["F3"], "task_ids": ["T1"]}, {"text": "구성안을 기획했습니다.", "task_ids": ["T2"]}]}]})
    screen = "\n".join(demo.render_draft(d, None))
    assert "(자격요건 근거 F3, 업무 연결 후보 T1)" in screen and "(업무 연결 후보 T2)" in screen and "사실 T" not in screen
    md = demo.save_draft_md(d, tmp_path).read_text(encoding="utf-8")
    assert "*(자격요건 근거 F3, 업무 연결 후보 T1)*" in md and "## 사용한 업무 연결 후보" in md
    assert "판정이 아니다" in md and f"- T1 {TASKS[0]}" in md and "evidence_detail" not in md
    plain = "\n".join(demo.render_draft(draft(para("글", fact_ids=["F1"]), links=False), None))
    assert "(자격요건 근거 F1)" in plain and "업무 연결 후보" not in plain


def test_policy_names_the_new_codes_and_the_candidate_rules():
    for phrase in ("missing_evidence", "unknown_task_id", "task_only_claim", "task_links", "업무 연결 후보", "표면 검사", "--task-links", "task_links_sample3.json"):
        assert phrase in POLICY, phrase


def test_link_rules_forbid_moving_task_wording_into_experience_sentences_only_when_links_exist():
    with_links, without = FakeClient([para("글", task_ids=["T1"])]), FakeClient([para("글", fact_ids=["F1"])])
    write_application(wi_for(), with_links, "cover_letter")
    write_application(wi_for(links=False), without, "cover_letter")
    system = with_links.requests[0].system
    for phrase in ("공고의 요구이지 지원자의 경험이 아니다", "경험의 주체, 대상, 행동, 결과", "그대로 쓴다", "카페 사장님", "입사 후 포부 문장에서만"):
        assert phrase in system, phrase
    assert "공고의 요구이지" not in (without.requests[0].system or "")  # 연결 후보가 없으면 이 문장도 들어가지 않는다
    assert with_links.requests[0].prompt_version == load_prompt(aw.STAGE["cover_letter"])[0] + "+links"


def test_policy_documents_the_task_wording_rule_and_its_limit():
    assert "D19" in POLICY and "공고의 요구이지 지원자의 경험이 아니다" in POLICY and "코드는 검증하지 못한다" in POLICY


LABELS = FIXTURE["labels"]
LABELED = build_links(PROFILE, TASKS, {"links": FIXTURE["links"]}, labels=LABELS)


def wi_labeled():
    sel = select_job(RUN, "sample-3", include_profile_evidence=True)
    return build_writer_input(sel, PROFILE, JobInfo(main_tasks=list(TASKS)), task_links=LABELED)


def test_the_fixture_has_one_label_per_task_and_links_carry_them():
    assert len(LABELS) == len(TASKS) == 5 and [t.label for t in LABELED] == LABELS


def test_with_labels_the_prompt_gets_the_label_but_not_the_task_sentence():
    client = FakeClient([para("글", task_ids=["T1"])])
    write_application(wi_labeled(), client, "cover_letter")
    req = client.requests[0]
    for label in LABELS:
        assert label in req.prompt
    for sentence in TASKS:  # 업무 문장은 프롬프트의 연결 후보에 없다
        assert sentence not in req.prompt.split("업무 연결 후보 (task_links")[1]
    assert req.prompt_version == load_prompt(aw.STAGE["cover_letter"])[0] + "+labels"
    assert "공고가 업무에 붙인 이름" in req.system and "입사 후 포부 문장에서만" in req.system


def test_without_labels_nothing_changes_from_the_task_sentence_version():
    client = FakeClient([para("글", task_ids=["T1"])])
    write_application(wi_for(), client, "cover_letter")
    req = client.requests[0]
    assert TASKS[0] in req.prompt and "label" not in req.prompt and req.prompt_version.endswith("+links")
    assert "공고가 업무에 붙인 이름" not in req.system


def test_labels_do_not_change_what_the_checker_accepts():
    wi = wi_labeled()
    ok = write_application(wi, FakeClient([para("정보 구조를 설계한 경험입니다.", task_ids=["T1"])]), "cover_letter")
    assert ok.linked_tasks[0].text == TASKS[0]  # 화면·md에는 업무 문장이 그대로 나온다
    with pytest.raises(DraftError) as e:
        write_application(wi, FakeClient([para("Figma로 화면을 그렸습니다.", task_ids=["T1"])]), "cover_letter")
    assert e.value.code == "unsupported_token"


def test_labels_must_match_the_number_of_tasks():
    with pytest.raises(ValueError):
        build_links(PROFILE, TASKS, {"links": FIXTURE["links"]}, labels=LABELS[:3])


def test_policy_documents_the_task_labels():
    assert "D20" in POLICY and "`label`" in POLICY and "업무 이름" in POLICY

