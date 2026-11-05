"""tools.eval_gate의 샘플 6건 모드와 --ai 연결 검증 (정책 job-gate.md 6-3, 6-5, 8장; 이슈 #12). 네트워크를 쓰지 않는다."""
import json
import re

import pytest

from core import job_gate as gate
from core.job_gate_mock import make_label_mock
from core.llm_client import GeminiClient, LLMQuotaExceeded, LLMStageError, UsageLog
from tools import eval_gate

AMBIGUOUS_A = [{"heading": "", "text": "회사의 다양한 업무를 지원하고 필요한 일을 수행"}, {"heading": "", "text": "기타 관련 업무 전반"}]
AMBIGUOUS_B = [{"heading": "", "text": "여러 부서의 요청을 받아 처리"}, {"heading": "", "text": "회사 전반의 잡다한 일을 맡아 수행"}]
SP, DEV = "JF_SERVICE_PLANNING", "JF_DEV"


def raw_case(no, items, company=None):
    return {"no": no, "company": company or f"회사{no}", "job_title": f"직무{no}", "position": f"포지션{no}", "items": items}


def expected_case(no, gate_result, core, labels, company=None):
    return {"no": no, "company": company or f"회사{no}", "expected": {"gate": gate_result, "core_family": core},
            "item_labels": labels, "excluded_item_count": 0}


@pytest.fixture
def sample_files(tmp_path, monkeypatch):
    """AI 자리를 부르는 합성 공고 2건(1번 서비스기획 일치, 2번 개발 불일치)을 샘플 입력으로 쓴다."""
    cases = tmp_path / "gate_sample_cases.json"
    expected = tmp_path / "expected.json"
    cases.write_text(json.dumps({"cases": [raw_case(1, AMBIGUOUS_A), raw_case(2, AMBIGUOUS_B)]}, ensure_ascii=False), encoding="utf-8")
    expected.write_text(json.dumps({"desired_family": SP, "cases": [
        expected_case(1, "match", SP, [SP, SP]), expected_case(2, "mismatch", DEV, [DEV, DEV])]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(eval_gate, "SAMPLE_CASES_JSON", cases)
    monkeypatch.setattr(eval_gate, "SAMPLE_EXPECTED_JSON", expected)
    return cases, expected


def main(*args):
    with pytest.raises(SystemExit) as e:
        eval_gate.main(list(args))
    return e.value.code


# ---------------------------------------------------------------- 입력 읽기

def test_load_sample_pairs_raw_and_expected_by_number(sample_files):
    desired, cases = eval_gate.load_sample()
    assert desired == SP and [c["id"] for c in cases] == ["sample-1", "sample-2"]
    assert cases[0]["expected_labels"] == [SP, SP] and cases[1]["expected_gate"] == "mismatch"


def test_load_sample_rejects_files_that_do_not_belong_together(tmp_path, sample_files):
    cases, expected = sample_files
    data = json.loads(expected.read_text(encoding="utf-8"))
    data["cases"][1]["company"] = "다른 회사"
    expected.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="2번.*회사"):
        eval_gate.load_sample()
    data["cases"][1]["company"] = "회사2"
    data["cases"][1]["item_labels"] = [DEV]
    expected.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="업무 항목 수"):
        eval_gate.load_sample()
    data["cases"][1]["no"] = 9
    expected.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="9번 공고가 없다"):
        eval_gate.load_sample()
    with pytest.raises(FileNotFoundError, match="팀 Drive"):
        eval_gate.load_sample(tmp_path / "없음.json")


def test_count_ai_calls_counts_the_requests_a_perfect_ai_would_receive(sample_files):
    desired, cases = eval_gate.load_sample()
    assert eval_gate.count_ai_calls(cases, desired) == 4  # 두 공고의 항목 2개씩이 모두 규칙으로 확정되지 않는다


# ---------------------------------------------------------------- mock 모드

def test_mock_sample_run_matches_and_never_touches_the_network(sample_files, monkeypatch, capsys):
    monkeypatch.setattr(eval_gate, "GeminiClient", lambda *a, **k: pytest.fail("mock 모드에서 클라이언트를 만들었다"))
    assert main("--set", "sample") == 0
    out = capsys.readouterr().out
    assert "공고 전체 일치 2/2" in out and "AI 호출(논리) 4회, AI 실패 0건" in out and "mock" in out


def test_missing_sample_file_gives_guidance(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(eval_gate, "SAMPLE_CASES_JSON", tmp_path / "없음.json")
    assert main("--set", "sample") == 2
    assert "팀 Drive" in capsys.readouterr().err


def test_default_set_is_still_dev_and_other_sets_are_untouched():
    parser_choices = eval_gate.DEFAULT_SET, eval_gate.CASES_JSON.name
    assert parser_choices == ("dev", "eval_cases.json")


# ---------------------------------------------------------------- 불일치 원인 범주 (정책 6-5)

def case_dict(items, labels, gate_result="match", core=SP):
    return {"id": "x", "job_title": "t", "position": "p", "items": items, "expected_gate": gate_result, "expected_core": core,
            "expected_labels": labels}


def run_one(case, judge):
    rows, _ = eval_gate.run_sample([case], SP, lambda c: judge)
    return rows[0]


def test_wrong_label_from_the_ai_is_an_ai_judgement_error():
    row = run_one(case_dict(AMBIGUOUS_A, [SP, SP]), lambda req: {"label": DEV, "reason": "x"})
    assert {cat for cat, _ in row["problems"]} == {eval_gate.CATEGORY_AI} and len(row["problems"]) == 2


def test_wrong_label_confirmed_by_rules_is_a_rule_error():
    items = [{"text": "서비스 기획 및 화면 설계"}, {"text": "요구사항 정의서 작성"}]
    row = run_one(case_dict(items, [DEV, DEV], "mismatch", DEV), make_label_mock([DEV, DEV]))
    assert row["requests"] == [] and {cat for cat, _ in row["problems"]} == {eval_gate.CATEGORY_RULE}


def test_expected_label_missing_from_the_allowed_list_is_a_candidate_miss():
    items = [{"text": "서비스 기획 및 백엔드 API 개발 병행"}, {"text": "데이터 분석과 영업 관리"}]
    row = run_one(case_dict(items, ["JF_SALES", "JF_SALES"], "mismatch", "JF_SALES"), lambda req: {"label": req["allowed"][0], "reason": "x"})
    assert eval_gate.CATEGORY_CANDIDATE in {cat for cat, _ in row["problems"]}


def test_all_items_right_but_final_decision_wrong_is_a_rule_error():
    row = run_one(case_dict(AMBIGUOUS_A, [SP, SP], "mismatch", SP), make_label_mock([SP, SP]))  # 기대 판정만 틀리게 적었다
    assert [(cat, text.split(" ")[0]) for cat, text in row["problems"]] == [(eval_gate.CATEGORY_RULE, "판정")]


def test_one_failed_call_fails_only_that_case_and_the_rest_continue(sample_files, capsys):
    desired, cases = eval_gate.load_sample()

    def judge_for(case):
        def judge(req):
            if case["id"] == "sample-1":
                raise LLMStageError("통신 실패 SECRET-DETAIL")
            return {"label": DEV, "reason": "x"}
        return judge

    rows, stopped = eval_gate.run_sample(cases, desired, judge_for)
    m = eval_gate.report_sample(rows, len(cases), "test")
    out = capsys.readouterr().out
    assert not stopped and [r["error"] for r in rows] == ["AI 호출 실패", None]
    assert m["failed"] == 1 and m["all_ok"] == 1 and m["done"] == 1
    assert "공고 전체 일치 1/2 (호출이 끝난 공고만 분모: 1/1)" in out and "호출 실패 1" in out and "SECRET-DETAIL" not in out


def test_quota_stops_the_run_and_leaves_the_rest_unstarted(sample_files, capsys):
    desired, cases = eval_gate.load_sample()

    def judge_for(case):
        def judge(req):
            raise LLMQuotaExceeded("일일 한도 SECRET-DETAIL")
        return judge

    rows, stopped = eval_gate.run_sample(cases, desired, judge_for)
    eval_gate.report_sample(rows, len(cases), "test")
    out = capsys.readouterr().out
    assert stopped and len(rows) == 1 and "시작하지 못한 공고 1건" in out and "SECRET-DETAIL" not in out


# ---------------------------------------------------------------- 실제 AI 경로 (진짜 GeminiClient + 가짜 생성 함수)

class FakeGemini(GeminiClient):
    """진짜 GeminiClient(재시도·사용량 기록·예산 점검)에 가짜 생성 함수를 끼운다. 실패는 fail_jobs에 job_id 접두사로 지정한다."""

    fail_jobs: tuple = ()
    quota_jobs: tuple = ()
    created = 0
    called = []

    def __init__(self, settings, usage=None):
        type(self).created += 1
        super().__init__(settings, usage, generate=self._fake_generate, sleep=lambda s: None)

    def _complete(self, request):
        if request.job_id.startswith(self.quota_jobs):  # 실제 클라이언트가 일일 한도 소진을 올리는 위치
            raise LLMQuotaExceeded("일일 한도")
        return super()._complete(request)

    def _fake_generate(self, request):
        type(self).called.append(request.job_id)
        if request.job_id.startswith(self.fail_jobs):
            raise LLMStageError("통신 실패")
        label = SP if request.job_id.startswith("sample-1:") else DEV
        return json.dumps({"label": label, "reason": "SECRET-REASON 결과물이 코드이다"}), 11, 7


@pytest.fixture
def ai_env(sample_files, tmp_path, monkeypatch):
    FakeGemini.fail_jobs, FakeGemini.quota_jobs, FakeGemini.created, FakeGemini.called = (), (), 0, []
    monkeypatch.setattr(eval_gate, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(eval_gate, "load_env", lambda: {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m-test", "GEMINI_RPM": "60", "GEMINI_RPD": "100"})
    monkeypatch.setattr(eval_gate, "GeminiClient", FakeGemini)
    return tmp_path / "results"


AI = ["--set", "sample", "--ai", "--accept-free-tier-terms"]


def test_ai_run_matches_prints_the_three_budget_numbers_and_caches_on_disk(ai_env, capsys):
    assert main(*AI) == 0
    out = capsys.readouterr().out
    assert "논리 호출 예상 4회" in out and "최대 시도 16회" in out and "오늘 이미 사용 0/100" in out  # 논리 4 x (1 + 재시도 3)
    assert "공고 전체 일치 2/2" in out and "AI m-test" in out and "토큰 입력 44 / 출력 28" in out
    assert len(list((ai_env / "llm_cache" / "gate").glob("*.json"))) == 4 and not (ai_env / "llm_cache" / "profile").exists()
    assert "SECRET-REASON" not in out  # 모델이 쓴 이유는 화면에 내지 않는다


def test_ai_call_ids_follow_the_posting_item_subtask_format_and_usage_has_no_content(ai_env):
    main(*AI)
    assert sorted(FakeGemini.called) == sorted(f"sample-{n}:item:{i}:subtask:0" for n in (1, 2) for i in (0, 1))
    rows = [json.loads(line) for line in (ai_env / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4 and all(r["stage"] == "gate_label" and re.fullmatch(r"sample-\d:item:\d:subtask:\d", r["job_id"]) for r in rows)
    text = (ai_env / "llm_usage.jsonl").read_text(encoding="utf-8")
    assert "SECRET-REASON" not in text and "회사의" not in text and "프롬프트" not in text  # 원문·이유·프롬프트가 없다


def test_second_run_with_the_same_cache_makes_no_calls_but_fresh_cache_does(ai_env, capsys):
    main(*AI)
    FakeGemini.called.clear()
    main(*AI)
    assert FakeGemini.called == []  # 같은 입력은 캐시 적중
    main(*AI, "--fresh-cache")
    assert len(FakeGemini.called) == 4 and "새 캐시" in capsys.readouterr().out


def test_without_the_consent_flag_nothing_is_sent(ai_env, capsys):
    assert main("--set", "sample", "--ai") == 2
    assert "--accept-free-tier-terms" in capsys.readouterr().err and FakeGemini.created == 0


@pytest.mark.parametrize("other", ["dev", "holdout", "all"])
def test_ai_is_refused_for_sets_other_than_sample(ai_env, capsys, other):
    assert main("--set", other, "--ai", "--accept-free-tier-terms") == 2
    assert "샘플 6건" in capsys.readouterr().err and FakeGemini.created == 0 and FakeGemini.called == []


def test_ai_with_the_default_set_is_refused_too(ai_env, capsys):
    assert main("--ai", "--accept-free-tier-terms") == 2
    assert FakeGemini.created == 0


def test_budget_counts_retries_and_what_was_already_used_today(ai_env, monkeypatch, capsys):
    usage = UsageLog(ai_env / "llm_usage.jsonl")
    request = type("R", (), {"stage": "gate_label", "job_id": "earlier", "prompt_version": "1"})()
    for _ in range(30):  # 오늘 이미 쓴 호출(자격요건 평가 등 같은 모델의 호출 포함)
        usage.record(request, model="m-test")
    # 남은 한도 = RPD - 30. 필요한 최대 시도 수는 16이다 -> 46이면 시작하고, 45면 시작하지 않는다
    for rpd, expect_code in ((45, 2), (46, 0)):
        monkeypatch.setattr(eval_gate, "load_env", lambda rpd=rpd: {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m-test", "GEMINI_RPM": "60", "GEMINI_RPD": str(rpd)})
        FakeGemini.called.clear()
        code = main(*AI, "--fresh-cache")
        err = capsys.readouterr()
        assert code == expect_code, (rpd, err.err)
        if expect_code == 2:
            assert "시작하지 않았다" in err.err and "필요한 호출 16회" in err.err and FakeGemini.called == []  # 아무것도 보내지 않았다


def test_a_call_failure_is_reported_per_job_and_exit_code_is_1(ai_env, capsys):
    FakeGemini.fail_jobs = ("sample-1:",)
    assert main(*AI) == 1
    out = capsys.readouterr().out
    assert "[sample-1] 실패: AI 호출 실패" in out and "[sample-2]" in out and "AI 실패 1건" in out and "통신 실패" not in out


def test_quota_exhaustion_stops_with_exit_code_3(ai_env, capsys):
    FakeGemini.quota_jobs = ("sample-1:",)
    assert main(*AI) == 3
    cap = capsys.readouterr()
    assert "시작하지 못한 공고 1건" in cap.out and "태평양" in cap.err and "[sample-2]" not in cap.out


def test_missing_sample_input_stops_before_any_client_is_made(ai_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(eval_gate, "SAMPLE_CASES_JSON", tmp_path / "없음.json")
    assert main(*AI) == 2
    assert "팀 Drive" in capsys.readouterr().err and FakeGemini.created == 0


def test_model_and_limit_flags_override_the_env(ai_env, capsys):
    assert main(*AI, "--model", "other-model", "--rpm", "30", "--rpd", "200") == 0
    assert "other-model" in capsys.readouterr().out


def test_the_ai_judge_in_the_run_is_the_llm_adapter_not_the_mock(ai_env):
    main(*AI)
    assert FakeGemini.called  # mock이면 GeminiClient가 불리지 않는다


def test_dev_and_holdout_evaluate_still_refuses_real_ai():
    with pytest.raises(NotImplementedError, match="범위 밖"):
        eval_gate.evaluate("dev", use_ai=True)


def test_mock_label_mock_and_adapter_share_the_ai_judge_shape():
    request = {"level": "subtask", "item_index": 0, "sub_index": 0, "text": "t", "item_text": "t", "other_items": [],
               "candidates": [], "allowed": [SP, DEV, "common", "out_of_scope", "mixed"], "families": gate.cfg.active_families()}
    assert set(make_label_mock([SP])(request)) == {"label", "reason"}
