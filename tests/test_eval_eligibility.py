"""샘플 6건 사슬 회귀와 tools/eval_eligibility.py 검증 (정책 eligibility.md 13-1, 17장, 18장; 이슈 #6). 네트워크를 쓰지 않는다."""
import json

import pytest

from core.llm_client import BaseClient, FakeClient, LLMQuotaExceeded, LLMResponse, LLMStageError
from core.llm_mock import evidence_answer, extraction_answer, synthetic_posting
from core.pipeline import assess_eligibility
from tools import eval_eligibility as tool

SAMPLES, PROFILE, EVIDENCE, GATES = tool.load_fixtures()
POLICY_14_ORDER = ["sample-3", "sample-1", "sample-4", "sample-5", "sample-2", "sample-6"]


# ---------------------------------------------------------------- 사슬: 추출 mock -> numeric 코드 -> 근거 mock -> build_result

def test_mock_chain_reproduces_policy_17_and_14(capsys):
    metrics, rows = tool.run_mock()
    n = len(SAMPLES)
    assert metrics["completed"] == n and metrics["failed"] == 0
    assert (metrics["sig_ok"], metrics["verdict_ok"], metrics["decisive_ok"]) == (n, n, n)
    assert metrics["status_ok"] == metrics["status_total"] == 30
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
    assert cmp["sig_ok"] is False and cmp["rows"] == [] and cmp["decisive_ok"] is None


# ---------------------------------------------------------------- 도구: 입력 점검

def test_missing_postings_file_gives_guidance(tmp_path):
    with pytest.raises(FileNotFoundError, match="공고 원문 파일이 없다"):
        tool.load_postings(tmp_path / "없음.json")


def test_load_postings_reads_job_id_to_text(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"postings": {"sample-1": {"text": "원문"}}}, ensure_ascii=False), encoding="utf-8")
    assert tool.load_postings(f) == {"sample-1": "원문"}


class Routing(BaseClient):
    """GeminiClient 대신 쓰는 가짜: 단계 이름으로 mock 정답을 골라 돌려주고, 외부 클라이언트인 척한다."""

    external = True

    def __init__(self, settings, usage=None):
        self.settings, self.model, self.calls = settings, settings.model, []
        self.answers = {}
        for s in SAMPLES:
            self.answers[("extract_requirements", s["job_id"])] = extraction_answer(s)
            self.answers[("find_evidence", s["job_id"])] = evidence_answer(s, EVIDENCE)

    def ensure_budget(self, needed):
        Routing.needed = needed

    def _complete(self, request):
        self.calls.append(request.stage)
        return LLMResponse(data=self.answers[(request.stage, request.job_id)])


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
    assert e.value.code == 1 and "[sample-2] 실패" in out and "완료 5/6" in out


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


def test_mock_output_has_no_posting_text_or_profile_quotes(capsys):
    tool.run_mock(verbose=True)
    out = capsys.readouterr().out
    pieces = [a for e in PROFILE.experiences for a in e.actions] + [e.title for e in PROFILE.experiences]
    assert len(pieces) > 5 and all(len(p) > 10 for p in pieces)
    assert all(piece not in out for piece in pieces)


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
