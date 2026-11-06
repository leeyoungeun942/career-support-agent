"""자소서·포트폴리오 초안 생성 검증 (정책 application-writing.md, 이슈 #14). 네트워크를 쓰지 않는다."""
import json
from pathlib import Path

import pytest
from pipeline_helpers import PROFILE, SAMPLES, Routing, main, run

from agents import application_writer as aw
from agents.application_writer import DraftError, make_writer_mock, write_application
from core.writer_input import ExperienceDetail
from core.llm_client import MAX_RETRIES, FakeClient, LLMQuotaExceeded, LLMResponse, LLMStageError
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


def response(*sections, heading="소제목"):
    """(섹션, 문단 글, fact_ids)마다 문단 하나짜리 섹션을 만든다."""
    return {"sections": [{"section": s, "heading": heading, "paragraphs": [{"text": t, "fact_ids": ids}]} for s, t, ids in sections]}


def shaped(section="motivation", heading="소제목", paragraphs=(), bullets=()):
    """문단과 불릿을 직접 지정하는 응답. 항목은 (글, fact_ids)."""
    sec = {"section": section, "heading": heading, "paragraphs": [{"text": t, "fact_ids": ids} for t, ids in paragraphs]}
    if bullets:
        sec["bullets"] = [{"text": t, "fact_ids": ids} for t, ids in bullets]
    return {"sections": [sec]}


def draft_of(resp, kind="cover_letter", job_id="sample-3"):
    return write_application(writer_input(job_id), FakeClient([resp]), kind)


def rejected(resp, kind="cover_letter", job_id="sample-3"):
    with pytest.raises(DraftError) as e:
        draft_of(resp, kind, job_id)
    return e.value.code


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
    used = {fid for s in draft.sections for item in [*s.paragraphs, *s.bullets] for fid in item.fact_ids}
    assert used and used <= wi.fact_ids
    assert all(s.heading and s.paragraphs for s in draft.sections)


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
    ([("motivation", "인턴 경험이 있습니다.", [])], "missing_evidence"),
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
    """'20'이나 '2026'은 인용한 사실의 '2026.02'의 일부라서 통과해서는 안 된다."""
    for bad in ("2026에 졸업했습니다.", "20에 졸업했습니다.", "2에 졸업했습니다."):
        with pytest.raises(DraftError):
            write_application(writer_input(), FakeClient([response(("motivation", bad, ["F2"]))]), "cover_letter")
    ok = write_application(writer_input(), FakeClient([response(("motivation", "2026.02에 졸업했습니다.", ["F2"]))]), "cover_letter")
    assert ok.sections[0].paragraphs[0].fact_ids == ["F2"]


def test_tokens_must_come_from_the_cited_facts_not_from_the_whole_input():
    """공고에만 있는 숫자, 인용하지 않은 프로필 경험의 도구명, 확인 필요 요건의 이름은 F1을 붙여도 거부한다."""
    wi = writer_input()
    assert any("4년" in r.text for r in wi.job.requirements) and "4" not in str(wi.usable_facts[0].model_dump())  # 4년은 F2의 요건에만 있다
    for section, text in (("job_experience", "4년 이상 경력이 있습니다."),
                          ("job_experience", "Notion으로 정리했습니다."),   # 프로필의 다른 경험(exp-4)에는 있지만 F1이 인용하지 않았다
                          ("job_experience", "SQL을 사용했습니다.")):
        with pytest.raises(DraftError) as e:
            write_application(wi, FakeClient([response((section, text, ["F1"]))]), "cover_letter")
        assert e.value.code == "unsupported_token"


def test_company_and_job_names_are_allowed_only_in_the_connection_sections():
    job = writer_input().job.model_copy(update={"job_title": "AIPlanner"})
    wi = writer_input().model_copy(update={"job": job})
    ok = write_application(wi, FakeClient([response(("motivation", "AIPlanner 직무에 지원합니다.", ["F1"]))]), "cover_letter")
    assert ok.sections[0].section == "motivation"
    with pytest.raises(DraftError):
        write_application(wi, FakeClient([response(("job_experience", "AIPlanner 직무에 지원합니다.", ["F1"]))]), "cover_letter")


def test_draft_states_that_meaning_was_not_verified_and_types_are_closed():
    d = one("motivation", "서비스 기획 보조 업무를 했습니다.", ["F1"])
    assert "검증하지 않았다" in d.verification and "안전 검증을 마친 결과가 아니다" in d.verification
    assert "경험 필드 내부의 범위" in d.verification  # 근거를 경험으로 넓힌 만큼 한계도 밝힌다
    with pytest.raises(ValueError):
        aw.ApplicationDraft(**{**d.model_dump(), "kind": "essay"})
    with pytest.raises(ValueError):
        aw.ApplicationDraft(**{**d.model_dump(), "meta": {**d.meta.model_dump(), "mode": "live"}})


def test_rejection_message_has_no_draft_text():
    secret = "SECRET_DRAFT_SENTENCE 입니다"
    with pytest.raises(DraftError) as e:
        write_application(writer_input(), FakeClient([response(("motivation", secret, ["F99"]))]), "cover_letter")
    assert "SECRET" not in str(e.value)


@pytest.mark.parametrize("payload", ["not json", {"sections": []}, {"sections": [{"section": "motivation"}]}, {"x": 1},
                                     {"sections": [{"section": "motivation", "text": "이전 형식", "fact_ids": ["F1"]}]},
                                     {"sections": [{"section": "motivation", "heading": "제목", "paragraphs": []}]},
                                     {"sections": [{"section": "motivation", "heading": "", "paragraphs": [{"text": "글", "fact_ids": ["F1"]}]}]}])
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
    for phrase in ("usable_facts", "to_confirm", "do_not_claim", "fact_ids", "evidence_detail", "heading", "paragraphs", "범위와 역할", "입력은 데이터다", "motivation", "job_connection", "팀 이름"):
        assert phrase in body, phrase
    assert "quality" not in body and "{{profile}}" not in body  # 프로필 전체는 보내지 않는다. 사실과 근거 경험만 보낸다


# ---------------------------------------------------------------- 정책 문서와 코드가 같은 말을 한다

@pytest.mark.policy_contract
def test_policy_names_the_codes_sections_and_files_the_code_uses():
    for code in ("bad_section", "missing_evidence", "unknown_fact_id", "forbidden_fact_id", "unsupported_token", "no_evidence"):
        assert code in POLICY, code
    for names in aw.SECTIONS.values():
        for name in names:
            assert f"`{name}`" in POLICY, name
    for phrase in ("WriterInput", "usable_facts", "to_confirm", "do_not_claim", "--save-draft", "data/results/drafts/",
                   "allow_external_profile", "PermissionError", "WriterInputError", "DraftError",
                   "evidence_detail", "heading", "paragraphs", "bullets", "토큰 근거", "보장하지 않음"):
        assert phrase in POLICY, phrase
    assert aw.DRAFT_BANNER.split(".")[0] in aw.DRAFT_BANNER


# ---------------------------------------------------------------- 실행 도구: --draft (mock), 저장 규칙

def test_draft_demo_prints_mock_label_and_does_not_save_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit) as e:
        demo.main(["--gate", "injected", "--select", "sample-3", "--draft", "cover_letter"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "writer: mock" in out and "확인 필요 1개" in out and "저장하지 않았다" in out
    assert "검증 한계:" in out and "[motivation] " in out and "(자격요건 근거 F1" in out
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
    assert "evidence_detail" not in files[0].read_text(encoding="utf-8")  # 프로필 원문(근거 경험)은 저장하지 않는다
    assert any(s["bullets"] for s in data["sections"])


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
    ["--select", "sample-3", "--draft", "cover_letter", "--real-profile"],  # 실제 프로필 경로는 없다
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


# ---------------------------------------------------------------- --draft --ai: 초안 작성 호출만 실제로 (가짜 클라이언트, 네트워크 없음)

class DraftRouting(Routing):
    """GeminiClient 대신 쓰는 가짜: 초안 작성 단계 요청에만 mock 초안을 돌려준다. 다른 단계가 오면 실패한다."""

    answer_override = None

    def __init__(self, settings, usage=None):
        super().__init__(settings, usage)
        self.usage = usage
        self.requests = []
        self.answers = {(f"write_{kind}", "sample-3"): MOCK_ANSWERS["sample-3"][kind] for kind in aw.KINDS}

    def _complete(self, request):
        self.requests.append(request)
        assert request.stage.startswith("write_") or request.stage == "link_tasks", f"초안 작성이나 연결 후보가 아닌 단계를 보냈다: {request.stage}"
        if request.stage == "link_tasks":
            data = {"links": json.loads(demo.TASK_LINKS_JSON.read_text(encoding="utf-8"))["sample-3"]["links"]}
        else:
            data = DraftRouting.answer_override or self.answers[(request.stage, request.job_id)]
        self.usage.record(request, model=self.model, input_tokens=11, output_tokens=5)
        return LLMResponse(data=data, input_tokens=11, output_tokens=5)


@pytest.fixture
def draft_ai(results, monkeypatch):
    DraftRouting.answer_override, DraftRouting.instances = None, []
    monkeypatch.setattr(demo, "load_env", lambda: {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m-test", "GEMINI_RPM": "5", "GEMINI_RPD": "20"})
    created = []

    def make(settings, usage=None):
        created.append(DraftRouting(settings, usage))
        return created[-1]

    monkeypatch.setattr(demo, "GeminiClient", make)
    return created


AI = ["--ai", "--accept-free-tier-terms", "--gate", "injected", "--select", "sample-3"]


def test_draft_ai_without_consent_sends_nothing(draft_ai, monkeypatch, capsys):
    monkeypatch.setattr(demo, "GeminiClient", lambda *a, **k: pytest.fail("동의 없이 클라이언트를 만들었다"))
    assert main("--ai", "--gate", "injected", "--select", "sample-3", "--draft", "cover_letter") == 2
    assert "--accept-free-tier-terms" in capsys.readouterr().err


@pytest.mark.parametrize("kind", aw.KINDS)
def test_draft_ai_sends_only_the_writer_call_and_labels_it_ai(draft_ai, results, capsys, kind):
    assert main(*AI, "--draft", kind, "--save-draft") == 0
    out = capsys.readouterr().out
    client = draft_ai[0]
    assert [r.stage for r in client.requests] == [f"write_{kind}"]  # 자격요건·게이트는 보내지 않는다
    assert client.requests[0].contains_profile is True
    assert "writer: ai (m-test)" in out and "자격요건과 게이트는 mock" in out and "이번 실행" in out
    files = list((results / "drafts").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text(encoding="utf-8"))["meta"]["mode"] == "ai"
    assert not (results / "llm_cache" / "profile").exists()  # 프로필이 든 요청은 디스크 캐시에 남지 않는다
    assert [r.get("ok", True) for r in map(json.loads, (results / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines())] == [True]


def test_draft_ai_checks_the_budget_for_one_call_with_retries(draft_ai, capsys):
    main(*AI, "--draft", "cover_letter")
    assert Routing.needed == 1 + MAX_RETRIES


def test_draft_ai_stops_before_any_call_when_budget_is_short(draft_ai, monkeypatch, capsys):
    class NoBudget(DraftRouting):
        def ensure_budget(self, needed):
            raise LLMQuotaExceeded("오늘 남은 호출 0회, 필요한 호출 4회")

    monkeypatch.setattr(demo, "GeminiClient", lambda settings, usage=None: NoBudget(settings, usage))
    assert main(*AI, "--draft", "cover_letter") == 2
    assert "시작하지 않았다" in capsys.readouterr().err


def test_draft_ai_rejects_a_model_draft_that_breaks_the_rules_and_saves_nothing(draft_ai, results, capsys):
    DraftRouting.answer_override = response(("motivation", "SECRET_MODEL_TEXT 인턴 경험이 있습니다.", ["X9"]))
    assert main(*AI, "--draft", "cover_letter", "--save-draft") == 2
    captured = capsys.readouterr()
    assert "DraftError" in captured.err and "SECRET_MODEL_TEXT" not in captured.out + captured.err
    assert not (results / "drafts").exists()


def test_draft_ai_quota_exhaustion_during_the_call_is_reported_not_traced(draft_ai, monkeypatch, capsys):
    class Exhausted(DraftRouting):
        def _complete(self, request):
            raise LLMQuotaExceeded("일일 한도")

    monkeypatch.setattr(demo, "GeminiClient", lambda settings, usage=None: Exhausted(settings, usage))
    assert main(*AI, "--draft", "cover_letter") == 2
    assert "Traceback" not in capsys.readouterr().err


# ---------------------------------------------------------------- 사실의 근거 경험 evidence_detail (정책 2장, #21)

def test_evidence_detail_is_a_list_with_every_cited_experience_and_only_the_four_fields():
    wi = writer_input("sample-3")
    facts = {f.fact_id: f for f in wi.usable_facts}
    assert [d.experience_id for d in facts["F3"].evidence_detail] == ["exp-2", "exp-3", "exp-4", "exp-5"]  # 인용된 경험 모두
    assert [d.experience_id for d in facts["F1"].evidence_detail] == ["exp-1"]  # 인용하지 않은 경험은 붙지 않는다
    assert set(ExperienceDetail.model_fields) == {"experience_id", "role", "actions", "tools", "result"}
    dumped = facts["F3"].model_dump(include={"evidence_detail"})
    assert all(set(d) == {"experience_id", "role", "actions", "tools", "result"} for d in dumped["evidence_detail"])
    text = json.dumps(dumped, ensure_ascii=False)
    for leaked in ("title", "period", "category", "2025-", "[학회]"):  # 제목·기간·분류값은 붙지 않는다
        assert leaked not in text, leaked


def test_a_fact_backed_by_a_profile_field_gets_no_evidence_detail():
    wi = writer_input("sample-1")
    f1 = next(f for f in wi.usable_facts if f.fact_id == "F1")
    assert f1.evidence[0].field == "education[0]" and f1.evidence_detail == []


def test_evidence_detail_is_not_in_errors_or_the_saved_draft(tmp_path):
    wi = writer_input("sample-3")
    secrets = [x for f in wi.usable_facts for d in f.evidence_detail for x in [*d.actions, d.result or ""] if x]
    assert secrets
    for resp in (response(("motivation", "인턴 경험이 있습니다.", ["F99"])), response(("motivation", "Kubernetes를 썼습니다.", ["F3"])),
                 response(("cover", "인턴 경험이 있습니다.", ["F3"]))):
        with pytest.raises(DraftError) as e:
            write_application(wi, FakeClient([resp]), "cover_letter")
        assert not any(secret in str(e.value) for secret in secrets)
    path = demo.save_draft(write_application(wi, make_writer_mock(MOCK_ANSWERS), "cover_letter"), tmp_path)
    assert "evidence_detail" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 토큰 근거: 단위의 fact_ids 합집합 (정책 3장, #21)

def test_experience_tokens_are_allowed_only_with_the_fact_that_cites_that_experience():
    # Figma는 exp-1(F1)의 도구, Excel은 exp-4(F3)의 도구, ATS는 exp-5(F3)의 행동
    assert draft_of(shaped(paragraphs=[("Figma로 화면을 그렸습니다.", ["F1"])]))
    assert draft_of(shaped(paragraphs=[("Excel과 ATS 기준을 썼습니다.", ["F3"])]))
    assert rejected(shaped(paragraphs=[("Figma로 화면을 그렸습니다.", ["F3"])])) == "unsupported_token"
    assert rejected(shaped(paragraphs=[("Excel과 ATS 기준을 썼습니다.", ["F1"])])) == "unsupported_token"
    assert rejected(shaped(paragraphs=[("Excel을 썼습니다.", ["F2"])])) == "unsupported_token"


def test_a_paragraph_with_two_facts_may_use_both_but_other_paragraphs_do_not_borrow():
    assert draft_of(shaped(paragraphs=[("Figma와 Excel을 썼습니다.", ["F1", "F3"])]))
    # 첫 문단의 F3은 둘째 문단의 근거가 아니다
    assert rejected(shaped(paragraphs=[("Excel을 썼습니다.", ["F3"]), ("Excel을 썼습니다.", ["F1"])])) == "unsupported_token"


def test_identifiers_and_metadata_are_not_token_evidence():
    for text in ("exp-2 경험입니다.", "education 경험입니다.", "F3 경험입니다.", "R3 경험입니다."):
        assert rejected(shaped(paragraphs=[(text, ["F3"])])) == "unsupported_token", text
    assert rejected(shaped(paragraphs=[("2025년에 했습니다.", ["F3"])])) == "unsupported_token"  # 기간(period)의 숫자는 근거가 아니다
    assert rejected(shaped(paragraphs=[("2025.07에 했습니다.", ["F1"])])) == "unsupported_token"


def test_job_requirement_numbers_are_not_evidence_even_with_the_experience_attached():
    assert rejected(shaped("job_experience", paragraphs=[("4년 이상의 경험이 있습니다.", ["F1"])])) == "unsupported_token"  # F1에는 exp-1이 붙어 있지만 4는 없다


# ---------------------------------------------------------------- 제목과 불릿도 같은 검증을 받는다 (#21)

def test_heading_tokens_are_checked_against_the_facts_used_in_the_section():
    assert draft_of(shaped(heading="약 200건의 응답을 분석했습니다", paragraphs=[("분석을 보조했습니다.", ["F1"])]))
    assert rejected(shaped(heading="200건 이상의 사용자 테스트 분석 경험", paragraphs=[("시나리오를 작성했습니다.", ["F3"])])) == "unsupported_token"
    assert rejected(shaped(heading="Kubernetes 운영 경험", paragraphs=[("시나리오를 작성했습니다.", ["F3"])])) == "unsupported_token"
    # 섹션의 문단이 쓴 사실의 합집합이면 통과한다
    assert draft_of(shaped(heading="Figma와 Excel", paragraphs=[("글", ["F1"]), ("글", ["F3"])]))


def test_headings_may_name_the_company_only_in_the_connection_sections():
    job = writer_input().job.model_copy(update={"company_name": "MaruCompany"})
    wi = writer_input().model_copy(update={"job": job})
    assert write_application(wi, FakeClient([shaped("motivation", heading="MaruCompany 지원동기", paragraphs=[("글", ["F1"])])]), "cover_letter")
    with pytest.raises(DraftError):
        write_application(wi, FakeClient([shaped("job_experience", heading="MaruCompany 경험", paragraphs=[("글", ["F1"])])]), "cover_letter")


def test_bullets_need_evidence_ids_and_are_checked_like_paragraphs():
    base = [("문단입니다.", ["F3"])]

    def check(bullet, job_id="sample-3"):
        return rejected(shaped("highlight_experiences", paragraphs=base, bullets=[bullet]), "portfolio", job_id)

    assert draft_of(shaped("highlight_experiences", paragraphs=base, bullets=[("시나리오 작성", ["F3"])]), "portfolio")
    assert check(("시나리오 작성", [])) == "missing_evidence"
    assert check(("시나리오 작성", ["F99"])) == "unknown_fact_id"
    assert check(("200건 이상 분석", ["F3"])) == "unsupported_token"
    assert check(("Kubernetes 운영", ["F3"])) == "unsupported_token"
    assert check(("글", ["C1"])) == "forbidden_fact_id"
    assert check(("글", ["X1"]), "sample-1") == "forbidden_fact_id"


def test_every_paragraph_needs_its_own_evidence_ids():
    assert rejected(shaped(paragraphs=[("첫 문단", ["F1"]), ("둘째 문단", [])])) == "missing_evidence"


def test_paragraph_count_is_a_prompt_recommendation_not_a_schema_rule():
    assert len(draft_of(shaped(paragraphs=[("문단 하나뿐입니다.", ["F1"])])).sections[0].paragraphs) == 1
    assert len(draft_of(shaped(paragraphs=[("문단", ["F1"])] * 5)).sections[0].paragraphs) == 5


# ---------------------------------------------------------------- 자소서에서 quality를 뺐다 (결정 D12)

def test_the_quality_section_is_gone_everywhere():
    assert "quality" not in aw.SECTIONS["cover_letter"] and len(aw.SECTIONS["cover_letter"]) == 3
    assert rejected(response(("quality", "꼼꼼하게 확인했습니다.", ["F1"]))) == "bad_section"
    assert "quality" not in json.dumps(MOCK_ANSWERS, ensure_ascii=False)
    for stage in aw.STAGE.values():
        assert "quality" not in load_prompt(stage)[1]


def test_prompt_data_has_facts_with_their_experience_but_not_the_whole_profile():
    client = FakeClient([response(("motivation", "인턴 경험이 있습니다.", ["F1"]))])
    write_application(writer_input("sample-3"), client, "cover_letter")
    prompt = client.requests[0].prompt
    assert "evidence_detail" in prompt and "Figma" in prompt and "Excel" in prompt  # 사실이 가리킨 경험의 내용은 간다
    assert "SQL 기초" not in prompt and "RAG 개념" not in prompt and "ADsP" not in prompt  # 프로필 전체(스킬, 자격증)는 가지 않는다
    assert "[개인]" not in prompt and "2025-" not in prompt  # 경험의 제목과 기간도 가지 않는다


def test_portfolio_screen_prints_headings_paragraphs_and_bullets(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit):
        demo.main(["--gate", "injected", "--select", "sample-3", "--draft", "portfolio"])
    out = capsys.readouterr().out
    assert "[highlight_experiences] 포트폴리오에서 강조하면 좋은 경험" in out and "    - 챗봇 시나리오와 예외 처리 가이드 작성" in out
    assert "(자격요건 근거 F3)" in out


# ---------------------------------------------------------------- 실제 AI 초안은 확인용 md를 쓴다 (정책 5장 D13, #23)

@pytest.mark.parametrize("kind", aw.KINDS)
def test_real_ai_draft_writes_a_review_md_without_the_save_flag(draft_ai, results, capsys, kind):
    assert main(*AI, "--draft", kind) == 0
    out = capsys.readouterr().out
    files = list((results / "drafts").glob("*"))
    assert [f.suffix for f in files] == [".md"] and files[0].name.endswith(f"-sample-3-{kind}.md")  # json은 --save-draft일 때만
    md = files[0].read_text(encoding="utf-8")
    draft = write_application(writer_input(), make_writer_mock(MOCK_ANSWERS), kind)
    assert md.startswith("# " + demo.KIND_LABEL[kind] + " 초안") and "writer: ai (m-test)" in md
    for sec in draft.sections:  # 소제목, 문단 전체, 불릿, 근거 사실 id가 모두 있다
        assert f"## {sec.heading}" in md
        assert all(para.text in md for para in sec.paragraphs) and all(b.text in md for b in sec.bullets)
    assert "자격요건 근거 F1" in md and "## 확인 필요 1개" in md and "C1 생성형 AI 도구를 자유자재로 활용" in md
    assert "검증 한계:" in md and aw.DRAFT_BANNER in md
    assert "확인용 md:" in out and files[0].name in out
    for secret in ("evidence_detail", "SQL 기초", "ADsP", "010", "김하늘"):  # 프로필 전체·식별 정보는 들어가지 않는다
        assert secret not in md


def test_real_ai_draft_md_is_not_written_when_the_draft_is_rejected(draft_ai, results, capsys):
    DraftRouting.answer_override = response(("motivation", "인턴 경험이 있습니다.", ["X9"]))
    assert main(*AI, "--draft", "cover_letter") == 2
    assert not (results / "drafts").exists()


def test_mock_draft_writes_md_only_with_the_save_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit):
        demo.main(["--gate", "injected", "--select", "sample-3", "--draft", "cover_letter"])
    assert not (tmp_path / "drafts").exists()
    with pytest.raises(SystemExit):
        demo.main(["--gate", "injected", "--select", "sample-3", "--draft", "cover_letter", "--save-draft"])
    assert sorted(f.suffix for f in (tmp_path / "drafts").glob("*")) == [".json", ".md"]


def test_review_md_is_not_written_for_a_real_profile_and_never_overwrites(tmp_path):
    draft = write_application(writer_input(), make_writer_mock(MOCK_ANSWERS), "portfolio")
    with pytest.raises(ValueError, match="가상 프로필"):
        demo.save_draft_md(draft.model_copy(update={"is_synthetic": False}), tmp_path)
    assert list(tmp_path.iterdir()) == []
    path = demo.save_draft_md(draft, tmp_path)
    assert "[highlight_experiences]" not in path.read_text(encoding="utf-8") and "(`highlight_experiences`)" in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        demo.save_draft_md(draft, tmp_path)


def test_policy_documents_the_review_md():
    assert "확인용 md" in POLICY and "D13" in POLICY and "--save-draft" in POLICY


# ---------------------------------------------------------------- --task-links ai: 연결 후보 호출 1회가 초안 호출 앞에 붙는다 (정책 5장 D18)

def test_task_links_ai_adds_one_call_before_the_draft_and_counts_it_in_the_budget(draft_ai, results, capsys):
    assert main(*AI, "--task-links", "ai", "--draft", "cover_letter") == 0
    client = draft_ai[0]
    assert [r.stage for r in client.requests] == ["link_tasks", "write_cover_letter"]
    assert all(r.contains_profile is True for r in client.requests)  # 둘 다 디스크 캐시에 안 남는다
    assert Routing.needed == 2 * (1 + MAX_RETRIES)  # 예산 점검에 연결 후보 호출을 반영한다
    assert not (results / "llm_cache" / "profile").exists()
    assert "자격요건과 게이트는 mock" in capsys.readouterr().out


def test_task_links_are_never_written_by_a_real_ai_run(draft_ai, results, capsys):
    assert main(*AI, "--task-links", "ai", "--draft", "cover_letter", "--save-draft") == 0
    capsys.readouterr()
    for path in results.rglob("*"):  # 연결 후보(프로필 원문)와 근거 경험은 어떤 파일에도 구조 그대로 남지 않는다
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "task_links" not in text and "evidence_detail" not in text, path
    usage = (results / "llm_usage.jsonl").read_text(encoding="utf-8")
    assert "카페" not in usage and "quote" not in usage  # 사용량 기록에는 내용이 없다


def test_task_links_none_is_the_default_and_sends_no_link_call(draft_ai, capsys):
    assert main(*AI, "--draft", "cover_letter") == 0
    assert [r.stage for r in draft_ai[0].requests] == ["write_cover_letter"]


def test_links_only_makes_one_link_call_shows_the_links_and_writes_no_draft(draft_ai, results, capsys):
    assert main(*AI, "--task-links", "ai", "--links-only", "--draft", "cover_letter") == 0
    out = capsys.readouterr().out
    assert [r.stage for r in draft_ai[0].requests] == ["link_tasks"]  # 초안 호출은 없다
    assert Routing.needed == 1 + MAX_RETRIES  # 예산도 연결 후보 호출 하나분
    assert "업무 연결 후보 5개 / 주요 업무 5개" in out and "저장하지 않았다" in out and "T1 " in out and "[exp-3]" in out
    assert not (results / "drafts").exists()  # 초안도 md도 쓰지 않는다


def test_links_only_needs_a_task_links_mode_and_prints_tasks_without_candidates():
    with pytest.raises(SystemExit) as e:
        demo.main(["--gate", "injected", "--select", "sample-3", "--draft", "cover_letter", "--links-only"])
    assert e.value.code == 2
    lines = demo.render_links([], ["업무 하나", "업무 둘"])
    assert lines[0].startswith("=== 업무 연결 후보 0개 / 주요 업무 2개") and lines[-1] == "후보가 없는 업무: T1, T2"

