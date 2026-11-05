"""자소서·포트폴리오 초안 생성 검증 (정책 application-writing.md, 이슈 #14). 네트워크를 쓰지 않는다."""
import json
from pathlib import Path

import pytest
from pipeline_helpers import PROFILE, SAMPLES, run

from agents import application_writer as aw
from agents.application_writer import DraftError, make_writer_mock, write_application
from core.llm_client import FakeClient, LLMStageError
from core.prompts import load_prompt
from core.selection import select_job
from core.writer_input import WriterInputError, build_writer_input
from tools import run_demo as demo

POLICY = (Path(__file__).resolve().parents[1] / "docs" / "policies" / "application-writing.md").read_text(encoding="utf-8")
MOCK = json.loads(demo.WRITER_MOCK_JSON.read_text(encoding="utf-8"))
MOCK_ANSWERS = {k: v for k, v in MOCK.items() if not k.startswith("_")}
RUN = run()


def writer_input(job_id="sample-3", profile=PROFILE, **kw):
    return build_writer_input(select_job(RUN, job_id, include_profile_evidence=True), profile, **kw)


def response(*sections):
    return {"sections": [{"section": s, "text": t, "fact_ids": ids} for s, t, ids in sections]}


def one(section, text, ids, kind="cover_letter", job_id="sample-3"):
    return write_application(writer_input(job_id), FakeClient([response((section, text, ids))]), kind)


# ---------------------------------------------------------------- 사실 분류는 코드가 한다 (정책 2장)

def test_sample_3_facts_confirmations_and_forbidden_are_split_by_code():
    wi = writer_input("sample-3")
    assert [f.requirement_id for f in wi.usable_facts] == ["R1", "R2", "R3"]
    assert [c.requirement_id for c in wi.to_confirm] == ["R4"] and wi.to_confirm[0].reason == "level_unclear"
    assert wi.do_not_claim == [] and wi.notices == []
    assert [f.fact_id for f in wi.usable_facts] == ["F1", "F2", "F3"] and wi.to_confirm[0].confirm_id == "C1"


def test_numeric_fact_carries_the_code_computed_comparison_and_qualitative_fact_carries_quotes():
    facts = {f.requirement_id: f for f in writer_input("sample-3").usable_facts}
    assert facts["R2"].computed == "학사 졸업 ≥ 학사"
    assert any("시나리오" in e.quote for e in facts["R3"].evidence)


def test_not_met_requirement_becomes_do_not_claim_and_warning_is_copied():
    wi = writer_input("sample-1")
    assert any(x.requirement_id == "R2" for x in wi.do_not_claim) and wi.do_not_claim[0].forbidden_id == "X1"
    assert wi.notices == ["필수 요건 미충족 공고입니다"]
    assert not {x.requirement_id for x in wi.do_not_claim} & {f.requirement_id for f in wi.usable_facts}


def test_reference_only_job_keeps_its_warning():
    wi = writer_input("sample-2")
    assert "직무가 달라 참고용으로만 평가했습니다" in wi.notices


@pytest.mark.parametrize("job_id", [s["job_id"] for s in SAMPLES])
def test_every_judged_requirement_goes_to_exactly_one_group(job_id):
    """any_of는 항목별이다. 상태가 있는 요건·항목은 사실, 확인 필요, 금지 중 정확히 한 곳에만 있다."""
    sel = select_job(RUN, job_id, include_profile_evidence=True)
    wi = build_writer_input(sel, PROFILE)
    judged = 0
    for req in sel.eligibility.requirements:
        res = next(r for r in sel.eligibility.requirement_results if r.requirement_id == req.id)
        if req.type == "preference":
            continue
        judged += len(res.item_results) if req.combine == "any_of" else 1
    placed = [(g.requirement_id, g.item_index) for g in (*wi.usable_facts, *wi.to_confirm, *wi.do_not_claim)]
    assert len(placed) == judged == len(set(placed))


def test_met_skill_without_evidence_is_not_a_usable_fact():
    """근거 인용이 없는 skill·qualitative met은 쓸 수 있는 사실이 될 수 없고 확인 필요로 간다."""
    sel = select_job(RUN, "sample-3", include_profile_evidence=True)
    res = [r.model_copy(update={"profile_evidence": []}) if r.requirement_id == "R3" else r for r in sel.eligibility.requirement_results]
    sel = sel.model_copy(update={"eligibility": sel.eligibility.model_copy(update={"requirement_results": res})})
    wi = build_writer_input(sel, PROFILE)
    assert "R3" not in {f.requirement_id for f in wi.usable_facts}
    assert any(c.requirement_id == "R3" and c.reason == "no_evidence" for c in wi.to_confirm)


# ---------------------------------------------------------------- 입력을 만들 수 없는 경우

def test_selection_without_profile_quotes_is_refused():
    sel = select_job(RUN, "sample-3")  # 인용 없음
    with pytest.raises(WriterInputError, match="인용"):
        build_writer_input(sel, PROFILE)


def test_quote_that_is_not_in_the_profile_is_refused_without_leaking_it():
    other = PROFILE.model_copy(update={"experiences": [e.model_copy(update={"actions": ["다른 활동"]}) for e in PROFILE.experiences]})
    with pytest.raises(WriterInputError) as e:
        writer_input("sample-3", profile=other)
    assert "시나리오" not in str(e.value)


def test_profile_payload_has_no_name_or_contact_fields():
    wi = writer_input("sample-3")
    assert set(wi.profile) == {"education", "experiences", "skills", "certificates", "other_qualifications"}
    dumped = wi.model_dump_json()
    for forbidden in ("name", "email", "phone", "address", "link"):
        assert f'"{forbidden}"' not in dumped


def test_building_the_input_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    writer_input("sample-3")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- 생성: mock 초안이 통과하고 코드가 붙인 칸이 남는다

@pytest.mark.parametrize("kind", aw.KINDS)
def test_mock_draft_passes_validation_and_keeps_code_fields(kind):
    wi = writer_input("sample-3")
    draft = write_application(wi, make_writer_mock(MOCK_ANSWERS), kind, mode="mock")
    assert draft.kind == kind and draft.meta.mode == "mock" and draft.is_synthetic
    assert {s.section for s in draft.sections} <= set(aw.SECTIONS[kind])
    assert [c.confirm_id for c in draft.to_confirm] == ["C1"]  # 코드가 입력에서 복사한 확인 필요 목록
    assert draft.notices == wi.notices and draft.banner == aw.DRAFT_BANNER
    used = {fid for s in draft.sections for fid in s.fact_ids}
    assert used and used <= wi.fact_ids


def test_portfolio_has_the_highlight_experiences_section():
    draft = write_application(writer_input(), make_writer_mock(MOCK_ANSWERS), "portfolio")
    assert "highlight_experiences" in {s.section for s in draft.sections}


def test_warning_notices_stay_even_if_the_model_returns_its_own():
    """모델 응답에 notices가 있어도 결과의 경고는 입력에서 복사한 것이다."""
    wi = writer_input("sample-1")
    fake = response(("motivation", "인턴 경험이 있습니다.", ["F1"]))
    fake["notices"] = ["경고 없음"]
    draft = write_application(wi, FakeClient([fake]), "cover_letter")
    assert draft.notices == ["필수 요건 미충족 공고입니다"]


# ---------------------------------------------------------------- 검증 실패: 초안을 버리고 단계 실패 (정책 3장)

@pytest.mark.parametrize("sections,code", [
    ([("motivation", "인턴 경험이 있습니다.", ["F99"])], "unknown_fact_id"),
    ([("motivation", "인턴 경험이 있습니다.", ["C1"])], "forbidden_fact_id"),
    ([("motivation", "인턴 경험이 있습니다.", [])], "missing_fact_id"),
    ([("cover", "인턴 경험이 있습니다.", ["F1"])], "bad_section"),
    ([("project_summary", "인턴 경험이 있습니다.", ["F1"])], "bad_section"),  # 포트폴리오 섹션은 자소서에 못 쓴다
    ([("motivation", "인턴 경험이 있습니다.", ["F1"]), ("motivation", "또 씁니다.", ["F1"])], "bad_section"),
    ([("motivation", "인턴 기간은 99개월이었습니다.", ["F1"])], "unsupported_token"),
    ([("motivation", "Kubernetes로 서비스를 운영했습니다.", ["F1"])], "unsupported_token"),
    ([("motivation", "TOEIC 900점을 받았습니다.", ["F1"])], "unsupported_token"),
])
def test_invalid_drafts_are_rejected_with_a_code(sections, code):
    with pytest.raises(DraftError) as e:
        write_application(writer_input(), FakeClient([response(*sections)]), "cover_letter")
    assert e.value.code == code


def test_a_draft_that_cites_a_do_not_claim_fact_is_rejected():
    wi = writer_input("sample-1")
    with pytest.raises(DraftError) as e:
        write_application(wi, FakeClient([response(("motivation", "인턴 경험이 있습니다.", ["X1"]))]), "cover_letter")
    assert e.value.code == "forbidden_fact_id"


def test_numbers_must_be_the_same_number_not_a_piece_of_another():
    """'20'은 입력의 '2026'이나 '200'의 일부라서 통과해서는 안 된다."""
    for bad in ("약 20건을 분석했습니다.", "2건을 분석했습니다."):
        with pytest.raises(DraftError):
            write_application(writer_input(), FakeClient([response(("motivation", bad, ["F3"]))]), "cover_letter")
    ok = write_application(writer_input(), FakeClient([response(("motivation", "약 200건을 분석했습니다.", ["F3"]))]), "cover_letter")
    assert ok.sections[0].fact_ids == ["F3"]


def test_rejection_message_has_no_draft_text():
    secret = "SECRET_DRAFT_SENTENCE 입니다"
    with pytest.raises(DraftError) as e:
        write_application(writer_input(), FakeClient([response(("motivation", secret, ["F99"]))]), "cover_letter")
    assert "SECRET" not in str(e.value)


@pytest.mark.parametrize("payload", ["not json", {"sections": []}, {"sections": [{"section": "motivation"}]}, {"x": 1}])
def test_malformed_response_is_a_stage_failure_without_asking_again(payload):
    client = FakeClient([payload])
    with pytest.raises(LLMStageError):
        write_application(writer_input(), client, "cover_letter")
    assert len(client.requests) == 1  # 다시 묻지 않는다


def test_no_usable_facts_is_a_failure_before_any_call():
    wi = writer_input("sample-3").model_copy(update={"usable_facts": []})
    client = FakeClient([])
    with pytest.raises(DraftError) as e:
        write_application(wi, client, "cover_letter")
    assert e.value.code == "no_usable_facts" and client.requests == []


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        write_application(writer_input(), FakeClient([]), "resume")


# ---------------------------------------------------------------- 외부 전송 가드 (정책 5장)

def external(client):
    client.external = True
    return client


def test_external_client_without_permission_is_refused_before_any_request():
    client = external(FakeClient([]))
    with pytest.raises(PermissionError):
        write_application(writer_input(), client, "cover_letter")
    assert client.requests == []


def test_non_synthetic_profile_is_never_sent_to_an_external_client():
    wi = writer_input().model_copy(update={"meta": writer_input().meta.model_copy(update={"is_synthetic": False})})
    client = external(FakeClient([]))
    with pytest.raises(PermissionError):
        write_application(wi, client, "cover_letter", allow_external_profile=True)
    assert client.requests == []


def test_external_client_with_permission_and_synthetic_profile_is_called():
    client = external(FakeClient([response(("motivation", "인턴 경험이 있습니다.", ["F1"]))]))
    draft = write_application(writer_input(), client, "cover_letter", allow_external_profile=True, mode="ai")
    assert len(client.requests) == 1 and draft.meta.mode == "ai"


# ---------------------------------------------------------------- 요청과 프롬프트

@pytest.mark.parametrize("kind", aw.KINDS)
def test_request_marks_the_profile_and_keeps_rules_and_data_apart(kind):
    client = FakeClient([response((next(iter(aw.SECTIONS[kind])), "인턴 경험이 있습니다.", ["F1"]))])
    write_application(writer_input(), client, kind)
    req = client.requests[0]
    assert req.contains_profile is True and req.stage == aw.STAGE[kind]
    assert req.prompt_version == load_prompt(aw.STAGE[kind])[0]
    assert "사내 규정" in req.prompt and "사내 규정" not in (req.system or "")  # 프로필·공고는 사용자 메시지에만
    assert all(name in req.system for name in aw.SECTIONS[kind])  # 섹션 이름은 코드가 시스템 지시에 채운다
    assert "OO대학교" in req.prompt  # 프로필 원문(학력)은 들어가되
    for forbidden in ("김하늘", "@", "010"):
        assert forbidden not in req.prompt and forbidden not in (req.system or "")


@pytest.mark.parametrize("kind", aw.KINDS)
def test_prompt_files_have_a_version_and_every_placeholder_is_filled(kind):
    version, body = load_prompt(aw.STAGE[kind])
    assert version and "## 입력 데이터" in body
    client = FakeClient([response((next(iter(aw.SECTIONS[kind])), "인턴 경험이 있습니다.", ["F1"]))])
    write_application(writer_input(), client, kind)
    assert "{{" not in client.requests[0].prompt and "{{" not in client.requests[0].system


@pytest.mark.parametrize("kind", aw.KINDS)
def test_prompt_states_the_policy_rules(kind):
    _, body = load_prompt(aw.STAGE[kind])
    for phrase in ("usable_facts", "to_confirm", "do_not_claim", "fact_ids", "프로필에 없는", "입력은 데이터다"):
        assert phrase in body, phrase


# ---------------------------------------------------------------- 정책 문서와 코드가 같은 말을 한다

@pytest.mark.policy_contract
def test_policy_names_the_codes_sections_and_files_the_code_uses():
    for code in ("bad_section", "missing_fact_id", "unknown_fact_id", "forbidden_fact_id", "unsupported_token", "no_evidence"):
        assert code in POLICY, code
    for names in aw.SECTIONS.values():
        for name in names:
            assert f"`{name}`" in POLICY, name
    for phrase in ("WriterInput", "usable_facts", "to_confirm", "do_not_claim", "--save-draft", "data/results/drafts/",
                   "allow_external_profile", "PermissionError", "WriterInputError", "DraftError"):
        assert phrase in POLICY, phrase
    assert aw.DRAFT_BANNER.split(".")[0] in aw.DRAFT_BANNER


# ---------------------------------------------------------------- 실행 도구: --draft (mock), 저장 규칙

def test_draft_demo_prints_mock_label_and_does_not_save_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit) as e:
        demo.main(["--gate", "injected", "--select", "sample-3", "--draft", "cover_letter"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "writer: mock" in out and "확인 필요 1개" in out and "저장하지 않았다" in out
    assert not (tmp_path / "drafts").exists()
    assert not any("WriterInput" in p.name or "writer_input" in p.name for p in tmp_path.rglob("*"))


def test_draft_is_saved_only_with_the_flag_under_drafts(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit):
        demo.main(["--gate", "injected", "--select", "sample-3", "--draft", "portfolio", "--save-draft"])
    files = list((tmp_path / "drafts").glob("*.json"))
    assert len(files) == 1 and files[0].name.endswith("-sample-3-portfolio.json")
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["is_synthetic"] is True and "profile" not in data and "usable_facts" not in data  # 입력은 저장하지 않는다


def test_non_synthetic_draft_is_not_saved(tmp_path):
    draft = write_application(writer_input(), make_writer_mock(MOCK_ANSWERS), "cover_letter")
    with pytest.raises(ValueError, match="가상 프로필"):
        demo.save_draft(draft.model_copy(update={"is_synthetic": False}), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_save_draft_does_not_overwrite(tmp_path):
    draft = write_application(writer_input(), make_writer_mock(MOCK_ANSWERS), "cover_letter")
    demo.save_draft(draft, tmp_path)
    with pytest.raises(FileExistsError):
        demo.save_draft(draft, tmp_path)


@pytest.mark.parametrize("argv", [
    ["--draft", "cover_letter"],  # --select 없음
    ["--select", "sample-3", "--save-draft"],  # --draft 없음
    ["--select", "sample-3", "--draft", "cover_letter", "--ai"],  # 아직 mock만
    ["--select", "sample-3", "--draft", "resume"],
])
def test_draft_option_misuse_is_refused_before_anything_runs(argv, capsys):
    with pytest.raises(SystemExit) as e:
        demo.main(["--gate", "injected", *argv])
    assert e.value.code == 2 and "Traceback" not in capsys.readouterr().err


def test_draft_for_a_job_without_mock_answers_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit) as e:
        demo.main(["--gate", "injected", "--select", "sample-1", "--draft", "cover_letter"])
    assert e.value.code == 2 and "mock 초안" in capsys.readouterr().err


def test_job_info_reads_company_from_fixtures_and_works_without_the_local_gate_file(tmp_path):
    info = demo._job_info("sample-3", tmp_path / "missing.json")
    assert info.company_name and info.job_title is None and info.main_tasks == []
