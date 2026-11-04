"""샘플 6건 사슬 회귀와 tools/eval_eligibility.py 검증 (정책 eligibility.md 13-1, 17장, 18장; 이슈 #6). 네트워크를 쓰지 않는다."""
import json

import pytest

from core.llm_client import FakeClient, LLMQuotaExceeded, LLMStageError
from core.llm_mock import evidence_answer, extraction_answer, synthetic_posting
from core.pipeline import assess_eligibility
from pipeline_helpers import Routing
from tools import eval_eligibility as tool

SAMPLES, PROFILE, EVIDENCE, GATES = tool.load_fixtures()
POLICY_14_ORDER = ["sample-3", "sample-1", "sample-4", "sample-5", "sample-2", "sample-6"]


# ---------------------------------------------------------------- 사슬: 추출 mock -> numeric 코드 -> 근거 mock -> build_result

def test_mock_chain_reproduces_policy_17_and_14(capsys):
    metrics, rows = tool.run_mock()
    n = len(SAMPLES)
    assert metrics["completed"] == n and metrics["failed"] == 0
    assert (metrics["sig_ok"], metrics["verdict_ok"], metrics["decisive_ok"]) == (n, n, n)
    assert metrics["status_ok"] == metrics["status_total"] == 29  # 필수 요건 29개(우대 preference 1개는 비교 대상이 아니다)
    assert metrics["count_ok"] == n
    assert (metrics["matched"], metrics["expected_required"]) == (29, 29)
    assert metrics["unmatched_expected"] == metrics["unmatched_extracted"] == 0
    assert metrics["field_ok"] == metrics["field_total"] and metrics["field_total"] > 0
    assert metrics["extraction"]["extracted"] == metrics["extraction"]["retained"] == 30
    assert metrics["reason_ok"] == metrics["reason_total"] == 19
    assert metrics["ranked"] == POLICY_14_ORDER
    out = capsys.readouterr().out
    assert "종합 판정 일치 6/6" in out and "mock" in out and "LLM 품질은 보장하지 않는다" in out


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["job_id"] for s in SAMPLES])
def test_chain_numeric_by_code_and_others_by_ai(sample):
    reqs, result = _assess(sample)
    for req, res in zip(reqs, result.requirement_results):
        if req.type == "numeric":
            assert (res.status_decided_by, res.evidence_checked_by) == ("rule", "rule"), req.id
        elif req.type in ("skill", "qualitative"):
            assert (res.status_decided_by, res.evidence_checked_by) == ("ai", "ai"), req.id
    assert result.extraction_status == "complete"


def _assess(sample, extract=None, evidence=None):
    posting = synthetic_posting(sample)
    mock_e = tool.make_extraction_mock([sample])
    mock_j = tool.make_evidence_mock([sample], EVIDENCE)
    return assess_eligibility(posting, GATES[sample["job_id"]], PROFILE, extract or mock_e, evidence or mock_j)


def test_extraction_failure_gives_conditional_without_calling_evidence_search():
    sample = SAMPLES[0]
    judge = FakeClient([])
    reqs, result = _assess(sample, extract=FakeClient([LLMStageError("통신 실패")]), evidence=judge)
    assert reqs == [] and judge.requests == []
    assert (result.verdict, result.extraction_status) == ("conditional", "incomplete")
    assert result.extraction_issues == ["extraction_failed"]


def test_evidence_stage_failure_is_not_silently_filled():
    with pytest.raises(LLMStageError):
        _assess(SAMPLES[0], evidence=FakeClient([LLMStageError("통신 실패")]))


def test_invented_quote_stops_the_result_from_being_built():
    sample = SAMPLES[0]
    bad = evidence_answer(sample, EVIDENCE)
    bad["judgements"][1]["profile_evidence"] = [{"experience_id": "exp-1", "quote": "프로필에 없는 문장"}]
    with pytest.raises(ValueError, match="프로필 근거 검증 실패"):
        _assess(sample, evidence=FakeClient([bad]))


def test_compare_sample_flags_different_requirement_structure():
    sample = SAMPLES[2]
    reqs, result = _assess(sample)
    other = SAMPLES[0]
    cmp = tool.compare_sample(other, reqs, result)
    assert cmp["sig_ok"] is False and cmp["only_want"] and cmp["matched"] < cmp["expected_required"]
    assert cmp["decisive_ok"] is False


# ---------------------------------------------------------------- 도구: 입력 점검

def test_missing_postings_file_gives_guidance(tmp_path):
    with pytest.raises(FileNotFoundError, match="공고 원문 파일이 없다"):
        tool.load_postings(tmp_path / "없음.json")


def test_load_postings_reads_job_id_to_text(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"postings": {"sample-1": {"text": "원문"}}}, ensure_ascii=False), encoding="utf-8")
    assert tool.load_postings(f) == {"sample-1": "원문"}


@pytest.fixture
def ai_env(tmp_path, monkeypatch):
    postings = tmp_path / "postings.json"
    postings.write_text(json.dumps({"postings": {s["job_id"]: {"text": synthetic_posting(s)} for s in SAMPLES}},
                                   ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(tool, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(tool, "load_env", lambda: {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m-test", "GEMINI_RPM": "5", "GEMINI_RPD": "20"})
    monkeypatch.setattr(tool, "GeminiClient", Routing)
    return tmp_path, postings


def ai_args(postings, *extra):
    return ["--ai", "--accept-free-tier-terms", "--postings", str(postings), *extra]


def test_ai_run_without_consent_flag_sends_nothing(ai_env, monkeypatch, capsys):
    monkeypatch.setattr(tool, "GeminiClient", lambda *a, **k: pytest.fail("동의 없이 클라이언트를 만들었다"))
    with pytest.raises(SystemExit) as e:
        tool.main(["--ai"])
    out = capsys.readouterr()
    assert e.value.code == 2 and "제품 개선" in out.out and "--accept-free-tier-terms" in out.err


def test_real_profile_flag_is_refused(ai_env, capsys):
    with pytest.raises(SystemExit) as e:
        tool.main(["--ai", "--accept-free-tier-terms", "--real-profile"])
    assert e.value.code == 2 and "구현하지 않았다" in capsys.readouterr().err


def test_ai_run_without_key_gives_guidance(ai_env, monkeypatch, capsys):
    tmp, postings = ai_env
    monkeypatch.setattr(tool, "load_env", lambda: {})
    with pytest.raises(SystemExit) as e:
        tool.main(ai_args(postings))
    assert e.value.code == 2 and "GEMINI_API_KEY" in capsys.readouterr().err


def test_ai_run_without_postings_file_gives_guidance(ai_env, capsys):
    tmp, _ = ai_env
    with pytest.raises(SystemExit) as e:
        tool.main(ai_args(tmp / "없음.json"))
    assert e.value.code == 2 and "공고 원문 파일이 없다" in capsys.readouterr().err


def test_ai_run_stops_before_any_call_when_budget_is_short(ai_env, monkeypatch, capsys):
    _, postings = ai_env

    class NoBudget(Routing):
        def ensure_budget(self, needed):
            raise LLMQuotaExceeded("오늘 남은 호출 3회, 필요한 호출 12회")

        def _complete(self, request):
            pytest.fail("한도가 모자란데 호출했다")

    monkeypatch.setattr(tool, "GeminiClient", NoBudget)
    with pytest.raises(SystemExit) as e:
        tool.main(ai_args(postings))
    assert e.value.code == 2 and "시작하지 않았다" in capsys.readouterr().err


def test_ai_run_with_routing_client_matches_policy_and_keeps_profile_off_disk(ai_env, capsys):
    tmp, postings = ai_env
    with pytest.raises(SystemExit) as e:
        tool.main(ai_args(postings))
    out = capsys.readouterr().out
    assert e.value.code == 0 and "종합 판정 일치 6/6" in out and "m-test" in out
    assert Routing.needed == 12  # 시작 전에 필요한 호출 수(공고 6 + 근거 6)를 점검한다
    assert not (tmp / "results" / "llm_cache" / "profile").exists()  # 프로필이 든 결과는 기본으로 디스크에 남지 않는다
    assert len(list((tmp / "results" / "llm_cache" / "extract").glob("*.json"))) == 6


def test_cache_profile_flag_writes_profile_cache_only_then(ai_env, capsys):
    tmp, postings = ai_env
    with pytest.raises(SystemExit):
        tool.main(ai_args(postings, "--cache-profile"))
    assert len(list((tmp / "results" / "llm_cache" / "profile").glob("*.json"))) == 6


def test_model_override_flags_reach_the_settings(ai_env, monkeypatch, capsys):
    _, postings = ai_env
    seen = {}

    class Spy(Routing):
        def __init__(self, settings, usage=None):
            super().__init__(settings, usage)
            seen["s"] = settings

    monkeypatch.setattr(tool, "GeminiClient", Spy)
    with pytest.raises(SystemExit):
        tool.main(ai_args(postings, "--model", "gemini-3.5-flash-lite", "--rpm", "15", "--rpd", "500"))
    assert (seen["s"].model, seen["s"].rpm, seen["s"].rpd) == ("gemini-3.5-flash-lite", 15, 500)


def test_ai_run_reports_stage_failure_per_sample_and_continues(ai_env, monkeypatch, capsys):
    _, postings = ai_env

    class Flaky(Routing):
        def _complete(self, request):
            if request.job_id == "sample-2" and request.stage == "find_evidence":
                raise LLMStageError("통신 실패")
            return super()._complete(request)

    monkeypatch.setattr(tool, "GeminiClient", Flaky)
    with pytest.raises(SystemExit) as e:
        tool.main(ai_args(postings))
    out = capsys.readouterr().out
    assert e.value.code == 1 and "[sample-2] 실패: 단계 실패(통신 또는 스키마)" in out and "완료 5/6" in out
    assert "통신 실패" not in out  # 예외 내용은 옮기지 않고 범주 이름만 낸다


def test_ai_run_stops_on_quota_exhaustion(ai_env, monkeypatch, capsys):
    _, postings = ai_env

    class Quota(Routing):
        def _complete(self, request):
            if request.job_id == "sample-3":
                raise LLMQuotaExceeded("일일 한도")
            return super()._complete(request)

    monkeypatch.setattr(tool, "GeminiClient", Quota)
    with pytest.raises(SystemExit) as e:
        tool.main(ai_args(postings))
    captured = capsys.readouterr()
    assert e.value.code == 3 and "[sample-3] 실패: 한도 소진" in captured.out and "[sample-4]" not in captured.out
    assert "일일 한도" not in captured.out


def test_mock_output_has_no_posting_text_or_profile_quotes(capsys):
    tool.run_mock(verbose=True)
    out = capsys.readouterr().out
    pieces = [a for e in PROFILE.experiences for a in e.actions] + [e.title for e in PROFILE.experiences]
    assert len(pieces) > 5 and all(len(p) > 10 for p in pieces)
    assert all(piece not in out for piece in pieces)
    # 요건 text(fixture의 요약문 포함)와 합성 공고 원문도 출력하지 않는다: 요건 id와 코드만 나온다
    assert all(r["text"] not in out for s in SAMPLES for r in s["requirements"] if len(r["text"]) > 6)
    assert "1-R2←R2" in out


# ---------------------------------------------------------------- 파이프라인이 단계 사이에 넘기는 값

def test_pipeline_passes_gate_core_family_to_extraction():
    sample = SAMPLES[1]  # 2번은 핵심 직무군이 JF_DEV인 불일치 공고
    spy = FakeClient([extraction_answer(sample)])
    judge = tool.make_evidence_mock([sample], EVIDENCE)
    assess_eligibility(synthetic_posting(sample), GATES[sample["job_id"]], PROFILE, spy, judge)
    assert "핵심 직무군: JF_DEV\n" in spy.requests[0].prompt


def test_pipeline_does_not_allow_external_profile_by_default():
    sample = SAMPLES[0]

    class External(FakeClient):
        external = True

    judge = External([evidence_answer(sample, EVIDENCE)])
    with pytest.raises(PermissionError):
        assess_eligibility(synthetic_posting(sample), GATES[sample["job_id"]], PROFILE, tool.make_extraction_mock([sample]), judge)
    assert judge.requests == []
    assess_eligibility(synthetic_posting(sample), GATES[sample["job_id"]], PROFILE, tool.make_extraction_mock([sample]), judge,
                       allow_external_profile=True)
    assert len(judge.requests) == 1


# ---------------------------------------------------------------- 실제 모델 결과 비교 방식 (필수 요건 기준)

def test_extra_preferred_requirements_do_not_break_comparison():
    sample = SAMPLES[2]  # 3번
    extra = {"level": "preferred", "type": "numeric", "numeric_kind": "career_years", "min_value": 1, "text": "경력 1년 이상"}
    sample_posting_extra = synthetic_posting(sample) + "\n우대사항:\n- 경력 1년 이상"

    answer = extraction_answer(sample)
    answer["requirements"].append(extra)  # 맨 뒤에 붙이므로 근거 mock의 R번호는 그대로다
    reqs, result = assess_eligibility(sample_posting_extra, GATES[sample["job_id"]], PROFILE, FakeClient([answer]),
                                      tool.make_evidence_mock([sample], EVIDENCE))
    cmp = tool.compare_sample(sample, reqs, result)
    assert cmp["matched"] == cmp["expected_required"] == 4 and cmp["only_want"] == [] and cmp["only_got"] == []
    assert cmp["decisive_ok"] and cmp["extra_other"] == 1 and cmp["field_ok"] == cmp["field_total"]
    assert all(p["result"].status == tool.expected_status(p["want"]) for p in cmp["rows"])


def test_different_type_on_a_matched_pair_is_a_field_difference_not_a_missing_requirement():
    sample = SAMPLES[3]
    reqs, result = _assess(sample)
    flipped = [r.model_copy(update={"type": "qualitative" if r.type == "skill" else r.type}) for r in reqs]
    cmp = tool.compare_sample(sample, flipped, result)
    assert cmp["matched"] == 3 and not cmp["sig_ok"] and cmp["count_ok"]
    assert [p["diffs"] for p in cmp["rows"]] == [[], ["type"], []]  # 4-R2(skill)만 qualitative로 바뀜


def test_fewer_required_requirements_than_expected_reports_the_missing_one_by_id():
    sample = SAMPLES[0]
    reqs, result = _assess(sample)
    cmp = tool.compare_sample(sample, reqs[:-1], result)
    assert cmp["only_want"] == ["1-R7"] and cmp["matched"] == 6 and not cmp["count_ok"] and cmp["positional_rows"] == []


# ---------------------------------------------------------------- 짝짓기는 순서에 의존하지 않는다 (PR #11 리뷰 P1-2)

def _required(sample):
    return [r for r in sample["requirements"] if r["level"] == "required"]


def test_matching_ignores_the_order_of_the_extracted_requirements():
    sample = SAMPLES[0]
    reqs, _ = _assess(sample)
    expected = _required(sample)
    forward, _, _ = tool.match_requirements(expected, reqs)
    reversed_reqs = list(reversed(reqs))
    pairs, only_w, only_g = tool.match_requirements(expected, reversed_reqs)
    assert only_w == only_g == [] and len(pairs) == len(expected)
    assert {(i, len(reqs) - 1 - j) for i, j in pairs} == set(forward)  # 같은 요건끼리 짝지어졌다


def test_a_merged_requirement_leaves_the_other_expected_one_unmatched():
    sample = SAMPLES[5]  # 6번: Excel과 PPT가 따로 있는 정답
    reqs, _ = _assess(sample)
    expected = _required(sample)
    merged = [r for r in reqs if r.id != "R3"]
    merged_excel = next(r for r in merged if r.id == "R2").model_copy(update={"text": "데이터 관리(Excel)와 보고서 작성(PPT)에 능숙하신 분"})
    got = [merged_excel if r.id == "R2" else r for r in merged]
    pairs, only_w, only_g = tool.match_requirements(expected, got)
    assert len(only_w) == 1 and only_g == [] and len(pairs) == 3


def test_unrelated_extracted_requirement_is_reported_as_extracted_only():
    sample = SAMPLES[3]
    reqs, _ = _assess(sample)
    expected = _required(sample)
    stray = reqs[1].model_copy(update={"id": "R9", "text": "전혀 다른 내용의 자격 문장입니다", "type": "qualitative"})
    pairs, only_w, only_g = tool.match_requirements(expected, [*reqs, stray])
    assert len(pairs) == 3 and only_w == [] and only_g == [3]


def test_field_diffs_compare_only_what_the_answer_specifies():
    sample = SAMPLES[0]
    reqs, _ = _assess(sample)
    career_want = next(r for r in _required(sample) if r.get("numeric_kind") == "career_years")
    career_got = next(r for r in reqs if r.numeric_kind == "career_years")
    assert tool.field_diffs(career_want, career_got) == ([], 4)  # type, numeric_kind, min_value, career_family (max_value는 정답에 없다)
    wrong = career_got.model_copy(update={"min_value": 1, "career_family": "JF_DEV"})
    assert set(tool.field_diffs(career_want, wrong)[0]) == {"min_value", "career_family"}


def test_ai_fresh_cache_uses_a_run_specific_directory_and_reports_run_totals(ai_env, capsys):
    tmp, postings = ai_env
    with pytest.raises(SystemExit) as e:
        tool.main(ai_args(postings, "--fresh-cache"))
    out = capsys.readouterr().out
    assert e.value.code == 0 and "새 캐시" in out and "이번 실행(" in out and "요건 추출 숫자(합계)" in out
    cache_root = tmp / "results" / "llm_cache"
    assert not (cache_root / "extract").exists()
    run_dirs = list(cache_root.glob("run-*"))
    assert len(run_dirs) == 1 and len(list(run_dirs[0].glob("*.json"))) == 6
