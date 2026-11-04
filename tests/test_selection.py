"""선택한 공고의 전달 형식 selected_job.v1 검증 (data-contract 4장, 정책 eligibility.md 16장; 이슈 #7). 초안 형식이다. 네트워크를 쓰지 않는다."""
import json

import pytest

from core.llm_client import LLMQuotaExceeded, LLMStageError
from core.pipeline import run_pipeline
from core.pipeline_run import ProfileRef, has_profile_evidence
from core.selection import WARNING_NOTICE, SelectedJob, SelectionRefused, select_job
from test_pipeline import PROFILE, REF, Wrap, clients, run, scenario
from tools import eval_eligibility as ev
from tools import run_demo as demo
from test_run_demo import Routing, ai_args, ai_env, main, results, saved_runs  # noqa: F401  (픽스처)


def quotes_in(selected):
    return has_profile_evidence(selected.eligibility)


# ---------------------------------------------------------------- 담기는 칸과 경고

def test_primary_job_without_problems_has_no_warning():
    r = run()
    sel = select_job(r, "sample-3", posting_ref="ref#sample-3")
    assert (sel.job_id, sel.run_id, sel.posting_ref) == ("sample-3", r.run_id, "ref#sample-3")
    assert sel.job_label == "샘플 3"
    assert sel.selection_warning_codes == [] and sel.selection_notices == []
    assert sel.gate.ranking_group == "primary" and sel.gate.result == "match"
    assert sel.eligibility.job_id == "sample-3" and sel.eligibility.verdict == "conditional"
    assert sel.profile == REF


@pytest.mark.parametrize("job_id,codes", [
    ("sample-1", ["eligibility_not_met"]),
    ("sample-4", ["reference_only"]),
    ("sample-2", ["reference_only", "eligibility_not_met"]),
])
def test_warning_codes_follow_the_result_and_texts_are_fixed(job_id, codes):
    sel = select_job(run(), job_id)
    assert sel.selection_warning_codes == codes
    assert sel.selection_notices == [WARNING_NOTICE[c] for c in codes]
    assert "직무가 달라 참고용으로만 평가했습니다" in WARNING_NOTICE.values()


def test_incomplete_extraction_adds_its_own_warning():
    mock_e, mock_j = clients()
    r = run(extract=Wrap(mock_e, {("extract_requirements", "sample-3"): LLMStageError("통신 실패")}), evidence=mock_j)
    sel = select_job(r, "sample-3")
    assert sel.eligibility.extraction_issues == ["extraction_failed"]
    assert sel.selection_warning_codes == ["extraction_incomplete"]


def test_gate_summary_has_no_detail_or_posting_items():
    dumped = select_job(run(), "sample-3").model_dump()
    assert set(dumped["gate"]) == {"result", "ranking_group", "core_family", "summary"}


def test_warnings_come_back_even_if_removed_from_the_file():
    data = json.loads(select_job(run(), "sample-2").model_dump_json())
    assert data["selection_warning_codes"] == ["reference_only", "eligibility_not_met"]
    data["selection_warning_codes"], data["selection_notices"] = [], []
    again = SelectedJob.model_validate(data)
    assert again.selection_warning_codes == ["reference_only", "eligibility_not_met"] and len(again.selection_notices) == 2


# ---------------------------------------------------------------- 선택할 수 없는 경우

def test_unknown_or_malformed_job_id_is_refused():
    for bad in ("sample-9", "../sample-3", "", "a/b", ".hidden"):
        with pytest.raises(SelectionRefused):
            select_job(run(), bad)


def test_failed_and_not_run_jobs_cannot_be_selected():
    mock_e, mock_j = clients()
    r = run(extract=mock_e, evidence=Wrap(mock_j, {("find_evidence", "sample-4"): LLMQuotaExceeded("한도 소진")}))
    with pytest.raises(SelectionRefused, match="평가 중단: 호출 한도 소진"):
        select_job(r, "sample-4")
    with pytest.raises(SelectionRefused, match="평가 안 함"):
        select_job(r, "sample-5")
    assert select_job(r, "sample-3").job_id == "sample-3"  # 앞 공고는 선택할 수 있다


# ---------------------------------------------------------------- 프로필 인용 (정책 16장)

def test_quotes_are_left_out_by_default_and_the_run_is_not_changed():
    r = run()
    sel = select_job(r, "sample-3")
    assert sel.profile_evidence_included is False and not quotes_in(sel)
    assert has_profile_evidence(r.jobs[2].eligibility)  # 원본 실행 결과는 그대로다


def test_quotes_are_included_only_when_asked_and_the_run_has_them():
    r = run()
    assert quotes_in(select_job(r, "sample-3", include_profile_evidence=True))
    with pytest.raises(SelectionRefused, match="저장돼 있지 않다"):
        select_job(r.without_profile_evidence(), "sample-3", include_profile_evidence=True)


def test_quotes_are_refused_for_a_real_profile():
    mock_e, mock_j = clients()
    real = run_pipeline(scenario(), PROFILE, ProfileRef(profile_id="real", is_synthetic=False), mock_e, mock_j)
    with pytest.raises(SelectionRefused, match="가상 프로필"):
        select_job(real, "sample-3", include_profile_evidence=True)
    assert select_job(real, "sample-3").profile_evidence_included is False  # 인용 없이 넘기는 것은 된다


def test_model_rejects_a_flag_that_hides_quotes():
    data = json.loads(select_job(run(), "sample-3", include_profile_evidence=True).model_dump_json())
    data["profile_evidence_included"] = False
    with pytest.raises(ValueError, match="프로필 인용"):
        SelectedJob.model_validate(data)


def test_model_rejects_mismatched_job_or_group():
    data = json.loads(select_job(run(), "sample-3").model_dump_json())
    with pytest.raises(ValueError, match="job_id"):
        SelectedJob.model_validate({**data, "job_id": "sample-1"})
    data["gate"]["ranking_group"] = "job_mismatch"
    with pytest.raises(ValueError, match="ranking_group"):
        SelectedJob.model_validate(data)


# ---------------------------------------------------------------- run_demo --select

def read_selection(results):
    files = sorted((results / "selection").glob("*.json"))
    assert len(files) == 1
    return files[0], demo.load_selection(files[0])


def test_cli_select_writes_a_file_without_quotes_and_says_so(results, capsys):
    assert main("--gate", "injected", "--select", "sample-3") == 0
    out = capsys.readouterr().out
    path, sel = read_selection(results)
    (run_file,) = saved_runs(results)
    assert path.name == f"{run_file.stem}-sample-3.json" and sel.run_id == run_file.stem  # 저장한 실행 결과와 같은 run_id
    assert sel.profile_evidence_included is False and not quotes_in(sel)
    assert "프로필 인용 제외" in out and "--save-profile-evidence" in out
    assert sel.posting_ref.startswith("synthetic-posting:sample-3")


def test_cli_select_with_flag_carries_quotes_and_prints_warnings(results, capsys):
    assert main("--gate", "injected", "--select", "sample-2", "--save-profile-evidence") == 0
    out = capsys.readouterr().out
    _, sel = read_selection(results)
    assert sel.profile_evidence_included and quotes_in(sel) and sel.profile.is_synthetic
    assert "경고: 직무가 달라 참고용으로만 평가했습니다" in out and "경고: 필수 요건 미충족 공고입니다" in out


def test_cli_select_unknown_job_stops_before_running(results, capsys):
    for bad in ("sample-9", "../x"):
        assert main("--gate", "injected", "--select", bad) == 2
        assert "실행할 수 없다" in capsys.readouterr().err
    assert not saved_runs(results) and not (results / "selection").exists()


def test_cli_select_never_overwrites(results):
    main("--gate", "injected", "--select", "sample-3")
    path, sel = read_selection(results)
    with pytest.raises(FileExistsError):
        demo.save_selection(sel, results / "selection")


def test_cli_ai_select_rejects_before_any_call_and_names_the_postings_file(ai_env, results, monkeypatch, capsys):
    class NoCall(Routing):
        def _complete(self, request):
            pytest.fail("선택이 틀렸는데 호출했다")

    monkeypatch.setattr(ev, "GeminiClient", NoCall)
    assert main(*ai_args(ai_env, "--select", "nope")) == 2
    assert not saved_runs(results)
    monkeypatch.setattr(ev, "GeminiClient", Routing)
    assert main(*ai_args(ai_env, "--select", "sample-3")) == 0
    _, sel = read_selection(results)
    assert sel.posting_ref.endswith("#sample-3") and "postings.json" in sel.posting_ref


def test_cli_select_of_a_failed_job_is_refused_but_the_run_is_kept(ai_env, results, monkeypatch, capsys):
    class Flaky(Routing):
        def _complete(self, request):
            if request.job_id == "sample-2" and request.stage == "find_evidence":
                raise LLMStageError("통신 실패")
            return super()._complete(request)

    monkeypatch.setattr(ev, "GeminiClient", Flaky)
    assert main(*ai_args(ai_env, "--select", "sample-2")) == 2
    assert "선택하지 않았다" in capsys.readouterr().err
    assert len(saved_runs(results)) == 1 and not (results / "selection").exists()
