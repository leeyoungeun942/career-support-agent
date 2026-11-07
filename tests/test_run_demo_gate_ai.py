"""tools/run_demo.py --gate ai: 게이트도 자격요건도 실제 AI (가짜 클라이언트, 네트워크 없음; 이슈 #16)."""
import json

import pytest

from core.job_gate_ai import STAGE as GATE_STAGE
from core.llm_client import MAX_RETRIES, LLMQuotaExceeded, LLMResponse, LLMStageError
from core.prompts import load_prompt
from core.selection import SelectionRefused, select_job
from pipeline_helpers import SAMPLES, Routing, main, saved_runs
from test_run_demo import write_gate_cases
from tools import eval_eligibility as ev
from tools import run_demo as demo

CONSENT = ["--ai", "--accept-free-tier-terms"]


def make_routing(gate_errors=None, left=None):
    """GeminiClient 대신 쓰는 가짜. 게이트 호출(job_id가 'sample-N:item:...')은 고정 라벨을, 자격요건 호출은 mock 정답을 돌려준다.

    gate_errors: {job_id 접두어: 예외}. left: 오늘 남은 호출 수(시작 전 점검용, None이면 무제한).
    """
    class GateRouting(Routing):
        gate_calls: list = []
        stages: list = []

        def __init__(self, settings, usage=None):
            super().__init__(settings, usage)
            self.usage = usage
            type(self).gate_calls, type(self).stages = [], []

        def ensure_budget(self, needed):
            super().ensure_budget(needed)
            if left is not None and needed > left:
                raise LLMQuotaExceeded(f"오늘 남은 호출 {left}회, 필요한 호출 {needed}회")

        def _complete(self, request):
            type(self).stages.append(request.stage)
            if request.stage == GATE_STAGE:
                type(self).gate_calls.append(request.job_id)
                for prefix, error in (gate_errors or {}).items():
                    if request.job_id.startswith(prefix):
                        self.usage.record(request, model=self.model, ok=False, error=type(error).__name__)
                        raise error
                data = {"label": "common", "reason": "테스트 이유 SECRET-REASON"}
            else:
                data = super()._complete(request).data
            self.usage.record(request, model=self.model, input_tokens=7, output_tokens=3)
            return LLMResponse(data=data, input_tokens=7, output_tokens=3)

    return GateRouting


@pytest.fixture
def gate_env(ai_postings, tmp_path):
    """--gate ai 실행 준비: conftest의 ai_postings(합성 공고 원문, 가짜 설정)에 합성 게이트 입력 파일을 더한다. 게이트 입력 경로를 돌려준다.

    두 파일이 같은 공고인지 보는 확인(게이트 입력의 직무명이 공고 원문에 있어야 한다)을 통과하도록 공고 원문에 직무명을 덧붙인다.
    """
    data = json.loads(ai_postings.read_text(encoding="utf-8"))
    for s in SAMPLES:
        data["postings"][s["job_id"]]["text"] += f"\n채용공고명: 직무{s['number']}"
    ai_postings.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return write_gate_cases(tmp_path / "gate_cases.json")


def args(postings, gate_cases, *extra):
    return [*CONSENT, "--gate", "ai", "--postings", str(postings), "--gate-cases", str(gate_cases), *extra]


def expected_budget(gate_cases):
    logical, gate_attempts, elig_attempts, total = demo.gate_ai_budget(type("A", (), {"gate_cases": str(gate_cases)})(), 6)
    assert (gate_attempts, elig_attempts, total) == (logical * (1 + MAX_RETRIES), 2 * 6 * (1 + MAX_RETRIES), logical * (1 + MAX_RETRIES) + 48)
    return logical, total


# ---------------------------------------------------------------- 옵션 조합: 아무것도 보내지 않는다

def test_gate_ai_without_ai_is_refused_and_nothing_is_sent(ai_postings, monkeypatch, capsys):
    monkeypatch.setattr(ev, "GeminiClient", lambda *a, **k: pytest.fail("--ai 없이 클라이언트를 만들었다"))
    assert main("--gate", "ai") == 2
    assert "--ai와 함께" in capsys.readouterr().err
    assert main("--accept-free-tier-terms", "--gate", "ai") == 2  # 동의 플래그만으로는 켜지지 않는다


def test_gate_ai_with_ai_but_no_consent_sends_nothing(gate_env, ai_postings, monkeypatch, capsys):
    monkeypatch.setattr(ev, "GeminiClient", lambda *a, **k: pytest.fail("동의 없이 클라이언트를 만들었다"))
    assert main("--ai", "--gate", "ai", "--postings", str(ai_postings), "--gate-cases", str(gate_env)) == 2
    assert "--accept-free-tier-terms" in capsys.readouterr().err


def test_gate_ai_with_draft_is_refused_before_any_dispatch(gate_env, ai_postings, monkeypatch, capsys):
    monkeypatch.setattr(ev, "GeminiClient", lambda *a, **k: pytest.fail("클라이언트를 만들었다"))
    monkeypatch.setattr(demo, "run_draft_ai", lambda a: pytest.fail("초안 AI 경로로 들어갔다"))
    monkeypatch.setattr(demo, "run_ai", lambda a: pytest.fail("게이트 AI 경로로 들어갔다"))
    monkeypatch.setattr(demo, "run_mock", lambda a: pytest.fail("mock 경로로 들어갔다"))
    for extra, message in ((["--ai"], "--draft와 함께 쓰지 않는다"), (["--ai", "--accept-free-tier-terms"], "--draft와 함께 쓰지 않는다"), ([], "--ai와 함께")):
        assert main("--gate", "ai", "--select", "sample-3", "--draft", "cover_letter", *extra) == 2
        assert message in capsys.readouterr().err


def test_draft_with_ai_still_goes_to_the_draft_path_when_the_gate_is_not_ai(monkeypatch):
    monkeypatch.setattr(demo, "run_draft_ai", lambda a: 77)
    assert main("--select", "sample-3", "--draft", "cover_letter", "--ai") == 77


def test_missing_gate_input_file_stops_before_the_notice_and_any_client(ai_postings, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ev, "GeminiClient", lambda *a, **k: pytest.fail("입력 파일이 없는데 클라이언트를 만들었다"))
    assert main(*CONSENT, "--gate", "ai", "--postings", str(ai_postings), "--gate-cases", str(tmp_path / "none.json")) == 2
    cap = capsys.readouterr()
    assert "샘플 공고 원문 파일이 없다" in cap.err and "무료 티어" not in cap.out


def test_gate_input_pairing_error_stops_the_run_before_any_call(gate_env, ai_postings, results, monkeypatch, capsys):
    Client = make_routing()
    monkeypatch.setattr(ev, "GeminiClient", Client)
    data = json.loads(gate_env.read_text(encoding="utf-8"))
    data["cases"][2]["company"] = "다른 회사"
    gate_env.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert main(*args(ai_postings, gate_env)) == 2
    assert "회사" in capsys.readouterr().err and not saved_runs(results) and Client.stages == []


# ---------------------------------------------------------------- 합산 호출 예산 점검 (경계)

def test_budget_check_adds_the_attempts_of_both_stages(gate_env, ai_postings, results, monkeypatch, capsys):
    logical, total = expected_budget(gate_env)
    Client = make_routing()
    monkeypatch.setattr(ev, "GeminiClient", Client)
    assert main(*args(ai_postings, gate_env)) == 0
    assert Routing.needed == total
    out = capsys.readouterr().out
    assert f"최대 시도 합계 {total}회" in out and f"게이트 최대 논리 {logical}회" in out


def test_run_starts_when_the_remaining_budget_equals_the_total(gate_env, ai_postings, results, monkeypatch):
    _, total = expected_budget(gate_env)
    monkeypatch.setattr(ev, "GeminiClient", make_routing(left=total))
    assert main(*args(ai_postings, gate_env)) == 0 and len(saved_runs(results)) == 1


@pytest.mark.parametrize("short", ["total_minus_one", "gate_part_only", "eligibility_part_only"])
def test_run_stops_before_any_call_when_one_attempt_short_or_only_one_stage_fits(short, gate_env, ai_postings, results, monkeypatch, capsys):
    logical, total = expected_budget(gate_env)
    left = {"total_minus_one": total - 1, "gate_part_only": logical * (1 + MAX_RETRIES), "eligibility_part_only": 48}[short]
    Client = make_routing(left=left)
    monkeypatch.setattr(ev, "GeminiClient", Client)
    assert main(*args(ai_postings, gate_env)) == 2
    assert "시작하지 않았다" in capsys.readouterr().err and not saved_runs(results) and Client.stages == []


# ---------------------------------------------------------------- 끝까지 실행: 기록과 저장

def test_full_run_uses_the_ai_for_both_stages_and_records_the_run(gate_env, ai_postings, results, monkeypatch, capsys):
    Client = make_routing()
    monkeypatch.setattr(ev, "GeminiClient", Client)
    monkeypatch.setattr(Routing, "config_id", "cfg-test", raising=False)
    assert main(*args(ai_postings, gate_env)) == 0
    cap = capsys.readouterr()
    header = next(line for line in cap.out.splitlines() if line.startswith("=== 시연"))
    assert "게이트: 실제 AI / 자격요건: 실제 AI" in header and "m-test" in header
    run = demo.load_run(saved_runs(results)[0])  # 저장 파일을 다시 읽어 검증한다
    info = run.run_info
    assert (info.gate_mode, info.gate_model_id, info.gate_prompt_version) == ("ai", "m-test", load_prompt("gate_label")[0])
    assert (info.eligibility_mode, info.model_id) == ("ai", "m-test") and run.counts == {"completed": 6, "failed": 0, "not_run": 0}
    gate_calls = Client.gate_calls
    # 합성 공고라 같은 요청이 여러 공고에서 나온다: 둘째부터는 캐시 적중이다. 논리 호출 = 시도(재시도 없음) + 캐시 적중
    assert info.gate_attempts == len(gate_calls) > 0 and info.gate_cache_hits > 0
    assert info.gate_logical_calls == info.gate_attempts + info.gate_cache_hits
    assert (info.gate_input_tokens, info.gate_output_tokens) == (7 * len(gate_calls), 3 * len(gate_calls))
    assert (info.eligibility_attempts, info.eligibility_logical_calls) == (12, 12) and info.input_tokens == 7 * 12  # 기존 칸은 자격요건 단계만
    assert "게이트 호출: 논리" in cap.out and "자격요건 호출: 논리 12회" in cap.out and f"시도 합계 {len(gate_calls) + 12}회" in cap.out
    assert "경고: 실제 호출이" not in cap.out
    assert (results / "llm_cache" / "gate").is_dir() and len(list((results / "llm_cache" / "extract").glob("*.json"))) == 6  # 캐시 디렉터리가 따로다
    assert not (results / "llm_cache" / "profile").exists()


def test_fresh_cache_gives_each_stage_its_own_new_directory(gate_env, ai_postings, results, monkeypatch):
    monkeypatch.setattr(ev, "GeminiClient", make_routing())
    assert main(*args(ai_postings, gate_env, "--fresh-cache")) == 0
    names = sorted(p.name for p in (results / "llm_cache").iterdir())
    assert len(names) == 2 and names[0].startswith("run-") and names[1] == names[0] + "-gate"


def test_saved_file_and_screen_have_no_model_reason_posting_text_or_error_text(gate_env, ai_postings, results, monkeypatch, capsys):
    monkeypatch.setattr(ev, "GeminiClient", make_routing({"sample-2:": LLMStageError("SECRET-ERROR")}))
    main(*args(ai_postings, gate_env, "--save-profile-evidence"))
    cap = capsys.readouterr()
    text = saved_runs(results)[0].read_text(encoding="utf-8")
    for secret in ("SECRET-REASON", "SECRET-ERROR", "직무1", "포지션1", "업무 0"):  # 모델 이유, 오류 내용, 게이트 입력(직무명·모집부문·업무 항목)
        assert secret not in text + cap.out + cap.err


# ---------------------------------------------------------------- 실패와 한도 소진

def test_gate_call_failure_keeps_the_other_jobs_and_exits_1(gate_env, ai_postings, results, monkeypatch, capsys):
    Client = make_routing({"sample-2:": LLMStageError("통신 실패 SECRET-DETAIL")})
    monkeypatch.setattr(ev, "GeminiClient", Client)
    assert main(*args(ai_postings, gate_env)) == 1
    cap = capsys.readouterr()
    assert "평가하지 못한 공고" in cap.out and "[sample-2]" in cap.out and "평가 실패: 게이트 AI 오류" in cap.out and "SECRET-DETAIL" not in cap.out + cap.err
    run = demo.load_run(saved_runs(results)[0])
    bad = next(j for j in run.jobs if j.job_id == "sample-2")
    assert (bad.evaluation_status, bad.failure_code, bad.gate, bad.eligibility) == ("failed", "gate_failed", None, None)
    assert run.counts == {"completed": 5, "failed": 1, "not_run": 0} and "sample-2" not in run.ranking
    assert any(c.startswith("sample-6:") for c in Client.gate_calls)  # 실패한 뒤 공고의 게이트도 돌았다
    assert Client.stages.count("find_evidence") == 5  # 게이트가 없는 공고는 자격요건 단계를 시작하지 않는다


def test_gate_stage_quota_exhaustion_stops_safely_keeps_earlier_gates_and_exits_3(gate_env, ai_postings, results, monkeypatch, capsys):
    Client = make_routing({"sample-3:": LLMQuotaExceeded("일일 한도 SECRET-DETAIL")})
    monkeypatch.setattr(ev, "GeminiClient", Client)
    assert main(*args(ai_postings, gate_env)) == 3
    cap = capsys.readouterr()
    assert "평가 중단: 호출 한도 소진" in cap.out and "평가 안 함: 호출 한도 소진" in cap.out and "SECRET-DETAIL" not in cap.out + cap.err and "태평양" in cap.err
    run = demo.load_run(saved_runs(results)[0])
    assert run.counts == {"completed": 0, "failed": 1, "not_run": 5}
    assert [j.gate is not None for j in run.jobs] == [True, True, False, False, False, False]  # 앞 공고의 게이트 결과는 남는다
    assert "extract_requirements" not in Client.stages and "find_evidence" not in Client.stages  # 자격요건 모델 호출 0회
    assert not any(c.startswith(("sample-4:", "sample-5:", "sample-6:")) for c in Client.gate_calls)  # 남은 공고의 게이트를 시작하지 않았다


# ---------------------------------------------------------------- --select 호환

def test_select_works_for_a_completed_job_and_refuses_a_gate_failed_one(gate_env, ai_postings, results, monkeypatch, capsys):
    monkeypatch.setattr(ev, "GeminiClient", make_routing({"sample-2:": LLMStageError("x")}))
    assert main(*args(ai_postings, gate_env, "--select", "sample-3")) == 1  # 실패 공고가 있어 종료 코드는 1이지만 선택은 저장된다
    assert "선택: sample-3" in capsys.readouterr().out and list((results / "selection").glob("*-sample-3.json"))
    assert main(*args(ai_postings, gate_env, "--select", "sample-2", "--fresh-cache")) == 2  # 앞 실행의 캐시로 실패가 가려지지 않게 새 캐시로 다시 한다
    err = capsys.readouterr().err
    assert "선택하지 않았다" in err and "평가 실패: 게이트 AI 오류" in err


def test_select_job_refuses_gate_failed_and_gate_stage_not_run():
    from test_pipeline_gate_ai import gate_run
    from core.llm_client import LLMStageError as Stage

    failed, _ = gate_run({"sample-2": Stage("x")})
    with pytest.raises(SelectionRefused, match="평가를 마치지 못해"):
        select_job(failed, "sample-2")
    exhausted, _ = gate_run({"sample-3": LLMQuotaExceeded("x")})
    for job_id in ("sample-1", "sample-3", "sample-6"):  # 게이트가 끝난 not_run, 소진된 공고, 시작하지 않은 공고
        with pytest.raises(SelectionRefused, match="평가를 마치지 못해"):
            select_job(exhausted, job_id)


def test_call_summary_warns_only_when_a_stage_went_over_its_computed_maximum():
    from pipeline_helpers import run

    base = run().run_info
    budget = (25, 100, 48, 148)
    ok = base.model_copy(update={"gate_logical_calls": 25, "gate_attempts": 30, "eligibility_logical_calls": 12, "eligibility_attempts": 12})
    assert not any("경고" in line for line in demo.render_gate_ai_calls(ok, budget))
    for change in ({"gate_logical_calls": 26}, {"gate_attempts": 101}, {"eligibility_attempts": 49}):
        over = ok.model_copy(update=change)
        assert any("경고: 실제 호출이" in line for line in demo.render_gate_ai_calls(over, budget))
