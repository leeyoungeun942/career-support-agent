"""tools/run_demo.py 검증 (data-contract 3장, 정책 eligibility.md 14·16·18장; 이슈 #7). 네트워크를 쓰지 않는다."""
import json
import subprocess
from pathlib import Path

import pytest

from core.llm_client import LLMQuotaExceeded, LLMResponse, LLMStageError
from core.pipeline import run_pipeline
from core.pipeline_run import ProfileRef
from pipeline_helpers import EVIDENCE, POLICY_14_ORDER, PROFILE, SAMPLES, Routing, ai_args, main, saved_runs
from tools import eval_eligibility as ev
from tools import run_demo as demo

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- mock 실행

def test_mock_run_prints_policy_order_and_marks_reference_only(results, capsys):
    assert main("--gate", "injected") == 0
    out = capsys.readouterr().out
    positions = [out.index(f"[{i}]") for i in POLICY_14_ORDER]
    assert positions == sorted(positions)
    assert out.count("[참고용 평가]") == 4 and "직무가 달라 참고용으로만 평가했습니다" in out
    assert out.index("직무가 달라 참고용으로만") > out.index("[sample-1]") and out.index("직무가 달라 참고용으로만") < out.index("[sample-4]")
    sample3 = out[out.index("[sample-3]"):out.index("[sample-1]")]
    assert sample3.count("확인 필요 1개") == 1  # 조건부 적합은 사유 문구에 이미 있어 개수를 덧붙이지 않는다
    assert "(확인 필요 3개)" in out[out.index("[sample-1]"):out.index("[sample-4]")]  # 미충족 공고는 확인 필요 개수를 덧붙인다
    assert "점수는 계산하지 않았다" in out and "자격요건: mock" in out and "게이트: fixture 결과 사용" in out


def test_screen_has_reasons_but_no_profile_quotes(results, capsys):
    main("--gate", "injected")
    out = capsys.readouterr().out
    assert "6개월 < 36개월(3년)" in out  # 1번의 결정적 사유
    pieces = [a for e in PROFILE.experiences for a in e.actions] + [e.title for e in PROFILE.experiences]
    assert len(pieces) > 5 and all(piece not in out for piece in pieces)


def test_quiet_prints_codes_only(results, capsys):
    assert main("--gate", "injected", "-q") == 0
    out = capsys.readouterr().out
    assert f"순위: {POLICY_14_ORDER}" in out and "1위 sample-3 primary conditional" in out
    assert "미충족" not in out and "6개월" not in out


def test_default_save_has_no_profile_quotes_and_reloads(results, capsys):
    main("--gate", "injected")
    (path,) = saved_runs(results)
    run = demo.load_run(path)
    assert run.profile_evidence_included is False and run.ranking == POLICY_14_ORDER
    assert path.stem == run.run_id and "저장:" in capsys.readouterr().out
    text = path.read_text(encoding="utf-8")
    pieces = [a for e in PROFILE.experiences for a in e.actions]
    assert all(piece not in text for piece in pieces) and '"profile_evidence": []' in text


def test_save_profile_evidence_flag_keeps_quotes(results, capsys):
    main("--gate", "injected", "--save-profile-evidence")
    (path,) = saved_runs(results)
    assert demo.load_run(path).profile_evidence_included is True
    assert "프로필 인용 포함" in capsys.readouterr().out


def test_every_run_gets_its_own_file_and_nothing_is_overwritten(results):
    main("--gate", "injected")
    (first,) = saved_runs(results)
    before = first.read_bytes()
    main("--gate", "injected")
    assert len(saved_runs(results)) == 2 and first.read_bytes() == before


def test_save_run_picks_a_new_run_id_when_the_name_is_taken(tmp_path):
    jobs, profile, desired, _ = demo.build_scenario("injected")
    run = run_pipeline(jobs, profile, demo.PROFILE_REF, ev.make_extraction_mock(SAMPLES), ev.make_evidence_mock(SAMPLES, EVIDENCE))
    p1, s1 = demo.save_run(run, tmp_path)
    before = p1.read_bytes()
    p2, s2 = demo.save_run(run, tmp_path)  # 같은 run_id
    assert p1 != p2 and s2.run_id != s1.run_id and p2.stem == s2.run_id
    assert p1.read_bytes() == before


def test_real_profile_evidence_cannot_be_saved(tmp_path):
    jobs, profile, desired, _ = demo.build_scenario("injected")
    run = run_pipeline(jobs, profile, ProfileRef(profile_id="real", is_synthetic=False),
                       ev.make_extraction_mock(SAMPLES), ev.make_evidence_mock(SAMPLES, EVIDENCE))
    with pytest.raises(ValueError, match="가상 프로필"):
        demo.save_run(run, tmp_path, save_profile_evidence=True)
    assert not list(tmp_path.glob("*.json"))
    demo.save_run(run, tmp_path)  # 인용을 빼고 저장하는 것은 된다


def test_result_files_are_git_ignored():
    r = subprocess.run(["git", "check-ignore", "-q", "data/results/pipeline/20261104T000000Z-abcdef.json"], cwd=REPO)
    assert r.returncode == 0


def test_real_profile_flag_is_refused(results, capsys):
    assert main("--real-profile") == 2
    assert "구현하지 않았다" in capsys.readouterr().err and not saved_runs(results)


# ---------------------------------------------------------------- 게이트 입력

def write_gate_cases(path, **change):
    cases = []
    for no, exp in {c["no"]: c for c in ev._read_json(ev.GATE_FIXTURE)["cases"]}.items():
        cases.append({"no": no, "company": exp["company"], "job_title": f"직무{no}", "position": f"포지션{no}",
                      "items": [{"text": f"업무 {k}"} for k in range(len(exp["item_labels"]))]})
    change.get("edit", lambda c: None)(cases)
    path.write_text(json.dumps({"desired_family": "JF_SERVICE_PLANNING", "cases": cases}, ensure_ascii=False), encoding="utf-8")
    return path


def test_auto_uses_fixture_gates_when_the_input_file_is_missing(results, tmp_path, capsys):
    assert main("--gate-cases", str(tmp_path / "none.json")) == 0
    assert "게이트: fixture 결과 사용" in capsys.readouterr().out


def test_mock_gate_without_the_input_file_is_refused(results, tmp_path, capsys):
    assert main("--gate", "mock", "--gate-cases", str(tmp_path / "none.json")) == 2
    assert "게이트 입력 파일이 없다" in capsys.readouterr().err and not saved_runs(results)


def test_gate_input_pairing_errors_stop_the_run_before_any_result(results, tmp_path, capsys):
    def edit_company(cases):
        cases[2]["company"] = "다른 회사"

    def drop_item(cases):
        cases[0]["items"].pop()

    def drop_case(cases):
        cases.pop()

    for edit, expect in ((edit_company, "회사가 기대 판정 fixture와 다르다"), (drop_item, "업무 항목 수"), (drop_case, "6번 공고가 없다")):
        path = write_gate_cases(tmp_path / "g.json", edit=edit)
        assert main("--gate", "mock", "--gate-cases", str(path)) == 2
        assert expect in capsys.readouterr().err
    assert not saved_runs(results)


def test_posting_title_must_appear_in_the_posting_text_when_pairing_with_postings(tmp_path):
    path = write_gate_cases(tmp_path / "g.json")
    texts = {s["job_id"]: f"채용공고명: 직무{s['number']}\n" for s in SAMPLES}
    jobs, *_ = demo.build_scenario("mock", path, texts)  # 짝이 맞으면 통과
    assert len(jobs) == 6 and all(j.gate is None and j.gate_judge for j in jobs)
    texts["sample-4"] = "채용공고명: 전혀 다른 공고"
    with pytest.raises(demo.ScenarioError, match="4번.*같은 공고가 아니다"):
        demo.build_scenario("mock", path, texts)


# ---------------------------------------------------------------- --ai (가짜 클라이언트, 네트워크 없음)

def test_ai_without_consent_sends_nothing(ai_postings, monkeypatch, capsys):
    monkeypatch.setattr(ev, "GeminiClient", lambda *a, **k: pytest.fail("동의 없이 클라이언트를 만들었다"))
    assert main("--ai", "--gate", "injected") == 2
    assert "--accept-free-tier-terms" in capsys.readouterr().err


def test_ai_run_changes_only_eligibility_and_records_run_info(ai_postings, results, monkeypatch, capsys):
    monkeypatch.setattr(Routing, "config_id", "cfg-test")

    class Counting(Routing):  # 실제 GeminiClient처럼 호출마다 사용량을 기록한다
        def __init__(self, settings, usage=None):
            super().__init__(settings, usage)
            self.usage = usage

        def _complete(self, request):
            data = super()._complete(request).data
            self.usage.record(request, model=self.model, input_tokens=7, output_tokens=3)
            return LLMResponse(data=data, input_tokens=7, output_tokens=3)

    monkeypatch.setattr(ev, "GeminiClient", Counting)
    assert main(*ai_args(ai_postings)) == 0
    out = capsys.readouterr().out
    assert "자격요건: AI m-test" in out and "게이트: fixture 결과 사용" in out and Routing.needed == 12
    run = demo.load_run(saved_runs(results)[0])
    info = run.run_info
    assert (info.eligibility_mode, info.model_id, info.cache_mode, info.gate_mode) == ("ai", "m-test", "memory", "injected")
    assert info.generation_config == "cfg-test" and run.ranking == POLICY_14_ORDER
    assert (info.input_tokens, info.output_tokens, info.cache_hits) == (7 * 12, 3 * 12, 0)  # 호출 12회(공고 6 + 근거 6)
    assert run.profile_evidence_included is False
    assert not (results / "llm_cache" / "profile").exists()  # 프로필이 든 결과는 디스크에 남지 않는다
    assert len(list((results / "llm_cache" / "extract").glob("*.json"))) == 6


def test_cache_flags_reach_the_run_info(ai_postings, results):
    main(*ai_args(ai_postings, "--fresh-cache"))
    main(*ai_args(ai_postings, "--cache-profile"))
    modes = [demo.load_run(p).run_info.cache_mode for p in saved_runs(results)]
    assert sorted(modes) == ["disk", "fresh"]
    assert len(list((results / "llm_cache" / "profile").glob("*.json"))) == 6


def test_ai_run_stops_before_any_call_when_budget_is_short(ai_postings, results, monkeypatch, capsys):
    class NoBudget(Routing):
        def ensure_budget(self, needed):
            raise LLMQuotaExceeded("오늘 남은 호출 3회, 필요한 호출 12회")

        def _complete(self, request):
            pytest.fail("한도가 모자란데 호출했다")

    monkeypatch.setattr(ev, "GeminiClient", NoBudget)
    assert main(*ai_args(ai_postings)) == 2
    assert "시작하지 않았다" in capsys.readouterr().err and not saved_runs(results)


def test_ai_stage_failure_keeps_the_other_jobs_and_exits_1(ai_postings, results, monkeypatch, capsys):
    class Flaky(Routing):
        def _complete(self, request):
            if request.job_id == "sample-2" and request.stage == "find_evidence":
                raise LLMStageError("통신 실패 SECRET-DETAIL")
            return super()._complete(request)

    monkeypatch.setattr(ev, "GeminiClient", Flaky)
    assert main(*ai_args(ai_postings)) == 1
    cap = capsys.readouterr()
    assert "평가하지 못한 공고" in cap.out and "평가 실패: AI 단계 오류" in cap.out and "SECRET-DETAIL" not in cap.out + cap.err
    run = demo.load_run(saved_runs(results)[0])
    assert run.counts == {"completed": 5, "failed": 1, "not_run": 0}


def test_ai_quota_exhaustion_marks_not_run_saves_and_exits_3(ai_postings, results, monkeypatch, capsys):
    class Quota(Routing):
        def _complete(self, request):
            if request.job_id == "sample-3":
                raise LLMQuotaExceeded("일일 한도 SECRET-DETAIL")
            return super()._complete(request)

    monkeypatch.setattr(ev, "GeminiClient", Quota)
    assert main(*ai_args(ai_postings)) == 3
    cap = capsys.readouterr()
    assert "평가 안 함: 호출 한도 소진" in cap.out and "SECRET-DETAIL" not in cap.out + cap.err and "태평양" in cap.err
    run = demo.load_run(saved_runs(results)[0])
    assert run.counts == {"completed": 2, "failed": 1, "not_run": 3} and run.ranking == ["sample-1", "sample-2"]


def test_ai_run_with_missing_postings_file_gives_guidance(ai_postings, tmp_path, capsys):
    assert main("--ai", "--accept-free-tier-terms", "--gate", "injected", "--postings", str(tmp_path / "none.json")) == 2
    assert "공고 원문 파일이 없다" in capsys.readouterr().err
