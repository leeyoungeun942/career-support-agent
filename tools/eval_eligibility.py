"""자격요건 평가 샘플 6건 실행 (docs/policies/eligibility.md 13-1, 17장, 18장).

사용법 (저장소 루트에서):
    python -m tools.eval_eligibility                 # mock (기본): fixture 정답을 돌려준다. 네트워크를 쓰지 않는다
    python -m tools.eval_eligibility -v              # 요건별 기대값과 결과를 모두 보여 준다
    python -m tools.eval_eligibility --ai --accept-free-tier-terms
                                                     # 실제 Gemini. 사용자가 직접 실행한다 (호출 12회, 가상 프로필과 샘플 공고만)
    python -m tools.eval_eligibility --ai --accept-free-tier-terms --model gemini-3.5-flash-lite --rpm 15 --rpd 500

mock의 의미: 정답을 그대로 돌려주므로 통과해도 LLM 품질은 보장하지 않는다. 파이프라인 연결과 코드 검증만 확인한다 (정책 18장).
--ai는 .env의 GEMINI_API_KEY, GEMINI_MODEL, GEMINI_RPM, GEMINI_RPD를 읽는다(--model, --rpm, --rpd로 덮어쓸 수 있다).
공고 원문은 data/sample-jobs/eligibility_sample_postings.json에서 읽는다(커밋하지 않는 파일). 형식:
    {"postings": {"sample-1": {"text": "공고 원문 ..."}, ..., "sample-6": {...}}}
프로필은 fixture의 가상 프로필(김하늘)만 쓴다. 다른 프로필 입력과 --real-profile은 구현하지 않았다 (정책 16장).
결과(프로필 인용이 든 판정)는 화면에만 출력하고 파일로 저장하지 않는다. 디스크에는 사용량 기록(숫자)과 공고 추출 캐시만 남는다.
"""
import argparse
import json
import sys
from pathlib import Path

from core.llm_client import (
    CachedClient,
    GeminiClient,
    GeminiSettings,
    LLMConfigError,
    LLMQuotaExceeded,
    LLMStageError,
    UsageLog,
    load_env,
)
from core.llm_mock import make_evidence_mock, make_extraction_mock, synthetic_posting
from core.pipeline import assess_eligibility
from core.rules import rank_jobs
from core.schemas import DISPLAY_LABELS, EligibilityProfile, JobGateResult

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
SAMPLE_FIXTURE = FIXTURES / "eligibility_sample_expected.json"
EVIDENCE_FIXTURE = FIXTURES / "eligibility_evidence_mock.json"
GATE_FIXTURE = FIXTURES / "job_gate_sample_expected.json"
POSTINGS_JSON = REPO / "data" / "sample-jobs" / "eligibility_sample_postings.json"
RESULTS_DIR = REPO / "data" / "results"  # 사용량 기록과 공고 추출 캐시(커밋하지 않음)

FREE_TIER_NOTICE = (
    "무료 티어로 보낸 입력은 공급자가 제품 개선에 쓰고 사람이 읽을 수 있다. 이 실행은 가상 프로필(김하늘)과 샘플 공고만 보낸다. "
    "실제 지원자 정보는 보내지 않는다 (정책 16장)."
)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixtures():
    """(표본 목록, 프로필, 근거 mock 판정, 게이트 결과 dict). 모두 커밋된 가상 데이터다."""
    data = _read_json(SAMPLE_FIXTURE)
    profile = EligibilityProfile(**{**data["profile"], "as_of_month": data["as_of_month"]})
    gate_cases = {c["no"]: c for c in _read_json(GATE_FIXTURE)["cases"]}
    desired = _read_json(GATE_FIXTURE)["desired_family"]
    gates = {}
    for s in data["samples"]:
        exp = gate_cases[s["number"]]["expected"]
        gates[s["job_id"]] = JobGateResult(job_id=s["job_id"], desired_family=desired, result=exp["gate"],
                                           core_family=exp.get("core_family"), summary="샘플")
        assert gates[s["job_id"]].ranking_group == s["gate_ranking_group"], f"{s['job_id']}: 게이트 그룹이 17장과 다르다"
    return data["samples"], profile, _read_json(EVIDENCE_FIXTURE)["judgements"], gates


def load_postings(path: Path = POSTINGS_JSON) -> dict[str, str]:
    """job_id -> 공고 원문. 파일이 없으면 FileNotFoundError(안내 문구 포함)."""
    if not Path(path).exists():
        raise FileNotFoundError(
            f"공고 원문 파일이 없다: {path}\n형식: " + '{"postings": {"sample-1": {"text": "..."}, ... "sample-6": {...}}}'
            "\n저장소에 올리지 않는 파일이다 (data/sample-jobs/는 .gitignore)."
        )
    return {k: v["text"] for k, v in _read_json(Path(path))["postings"].items()}


def _signature(items):
    return [(r["level"], r["type"], r.get("numeric_kind")) if isinstance(r, dict) else (r.level, r.type, r.numeric_kind) for r in items]


def compare_sample(sample, requirements, result):
    """기대(fixture)와 결과를 비교한다. 요건 수·유형 순서가 같을 때만 요건별 비교를 한다.

    돌려주는 값: sig_ok(요건 수·level·type·numeric_kind 일치), verdict_ok, decisive_ok(요건 위치로 비교), 요건별 (기대, 결과) 행.
    """
    expected = sample["requirements"]
    sig_ok = _signature(expected) == _signature(requirements)
    rows = []
    if sig_ok:
        for want, res in zip(expected, result.requirement_results):
            rows.append((want, res))
    decisive_ok = None
    if sig_ok:
        index = {r["id"]: i for i, r in enumerate(expected)}
        want_pos = [index[i] for i in sample["expected"]["decisive"]]
        got_pos = [next(i for i, r in enumerate(requirements) if r.id == d) for d in result.decisive_requirement_ids]
        decisive_ok = want_pos == got_pos
    return {"sig_ok": sig_ok, "verdict_ok": result.verdict == sample["expected"]["verdict"], "decisive_ok": decisive_ok, "rows": rows}


def expected_status(req):
    """fixture가 기대하는 요건 상태. any_of는 하나라도 met이면 met, 전부 not_met이면 not_met, 그 밖에는 needs_check."""
    if req["type"] == "preference":
        return None
    if "item_statuses" in req:
        statuses = [st for st, _ in req["item_statuses"]]
        return "met" if "met" in statuses else ("not_met" if set(statuses) == {"not_met"} else "needs_check")
    return req["status"]


def _label(code):
    return DISPLAY_LABELS.get(code, code) if code else "-"


def run_all(samples, profile, gates, postings, extract_client, evidence_client, allow_external_profile=False, verbose=False):
    """샘플을 차례로 평가해 (행 목록, 결과 목록, 중단 사유)를 돌려준다. 단계 실패는 그 공고만 실패로 기록하고 이어간다."""
    rows, results, stopped = [], [], None
    for s in samples:
        gate = gates[s["job_id"]]
        row = {"sample": s, "error": None, "cmp": None, "result": None}
        try:
            reqs, result = assess_eligibility(postings[s["job_id"]], gate, profile, extract_client, evidence_client,
                                              allow_external_profile)
            row["result"], row["cmp"] = result, compare_sample(s, reqs, result)
            row["requirements"] = reqs
            results.append(result)
        except LLMQuotaExceeded as e:
            row["error"], stopped = f"한도 소진: {e}", str(e)
            rows.append(row)
            break
        except LLMStageError as e:
            row["error"] = f"단계 실패: {e}"
        except ValueError as e:
            row["error"] = "근거 검증 실패(프로필 원문에 없는 인용 등)" if "프로필 근거 검증 실패" in str(e) else f"결과 조립 실패: {str(e)[:120]}"
        rows.append(row)
    return rows, results, stopped


def report(rows, results, gates, verbose=False):
    """요약을 출력하고 지표 dict를 돌려준다. 공고 원문·프로필 내용·모델 출력 원문은 출력하지 않는다(요건 text는 fixture 것만)."""
    n = len(rows)
    ok_rows = [r for r in rows if r["result"] is not None]
    m = {"samples": n, "completed": len(ok_rows), "failed": n - len(ok_rows),
         "sig_ok": sum(r["cmp"]["sig_ok"] for r in ok_rows), "verdict_ok": sum(r["cmp"]["verdict_ok"] for r in ok_rows),
         "decisive_ok": sum(bool(r["cmp"]["decisive_ok"]) for r in ok_rows), "status_ok": 0, "status_total": 0,
         "reason_ok": 0, "reason_total": 0}
    for r in rows:
        s = r["sample"]
        if r["error"]:
            print(f"[{s['job_id']}] 실패: {r['error']}")
            continue
        c, res = r["cmp"], r["result"]
        print(f"[{s['job_id']}] 종합 {_label(res.verdict)} (기대 {_label(s['expected']['verdict'])}) "
              f"| 요건 {len(res.requirements)}개 {'(기대와 같은 구성)' if c['sig_ok'] else '(기대와 구성이 다름: 요건별 비교 생략)'} "
              f"| {_label(res.extraction_status)}")
        for want, got in c["rows"]:
            m["status_total"] += 1
            same = got.status == expected_status(want)
            m["status_ok"] += same
            if want["type"] in ("skill", "qualitative") and "item_statuses" not in want:
                m["reason_total"] += 1
                m["reason_ok"] += got.reason_code == want.get("reason_code")
            if verbose or not same:
                print(f"   {'O' if same else 'X'} {want['id']} {want['text'][:30]} | 기대 {_label(expected_status(want))} → 결과 {_label(got.status)}"
                      f"{' / ' + _label(got.reason_code) if got.reason_code else ''}")
    ranked = [r.job_id for r in rank_jobs(results, [gates[r.job_id] for r in results])] if results else []
    print(f"\n정렬 결과: {ranked}")
    print(f"완료 {m['completed']}/{n}, 요건 구성 일치 {m['sig_ok']}/{m['completed']}, 종합 판정 일치 {m['verdict_ok']}/{m['completed']}, "
          f"결정적 요건 일치 {m['decisive_ok']}/{m['completed']}")
    print(f"요건별 상태 일치 {m['status_ok']}/{m['status_total']}, skill·qualitative 사유 일치 {m['reason_ok']}/{m['reason_total']}")
    m["ranked"] = ranked
    return m


def run_mock(verbose=False):
    samples, profile, evidence, gates = load_fixtures()
    postings = {s["job_id"]: synthetic_posting(s) for s in samples}
    extract = make_extraction_mock(samples)
    judge = make_evidence_mock(samples, evidence)
    print("=== 자격요건 평가 샘플 6건 (mock: 정답을 돌려준다. LLM 품질은 보장하지 않는다) ===")
    rows, results, _ = run_all(samples, profile, gates, postings, extract, judge)
    return report(rows, results, gates, verbose), rows


def run_ai(args):
    """실제 Gemini 실행. 가상 프로필과 샘플 공고만 보낸다. 호출 전에 설정, 공고 파일, 하루 예산을 모두 점검한다."""
    if args.real_profile:
        print("--real-profile은 구현하지 않았다. 실제 지원자 프로필은 팀의 별도 승인 뒤에도 이 도구로 보내지 않는다 (정책 16장).", file=sys.stderr)
        return 2
    print(FREE_TIER_NOTICE)
    if not args.accept_free_tier_terms:
        print("위 조건에 동의하면 --accept-free-tier-terms를 붙여 다시 실행하라. 아직 아무것도 보내지 않았다.", file=sys.stderr)
        return 2
    env = load_env()
    for flag, key in (("model", "GEMINI_MODEL"), ("rpm", "GEMINI_RPM"), ("rpd", "GEMINI_RPD")):
        if getattr(args, flag) is not None:
            env[key] = str(getattr(args, flag))
    try:
        settings = GeminiSettings.from_env(env)
        postings = load_postings(Path(args.postings))
    except (LLMConfigError, FileNotFoundError) as e:
        print(f"실행할 수 없다: {e}", file=sys.stderr)
        return 2
    samples, profile, _, gates = load_fixtures()
    missing = [s["job_id"] for s in samples if s["job_id"] not in postings]
    if missing:
        print(f"공고 원문이 없는 표본: {missing}", file=sys.stderr)
        return 2

    usage = UsageLog(RESULTS_DIR / "llm_usage.jsonl")
    gemini = GeminiClient(settings, usage)
    try:
        gemini.ensure_budget(2 * len(samples))
    except LLMQuotaExceeded as e:
        print(f"시작하지 않았다: {e}", file=sys.stderr)
        return 2
    before = usage.calls_today(settings.model)
    client = CachedClient(gemini, usage=usage, extract_dir=RESULTS_DIR / "llm_cache" / "extract",
                          profile_dir=RESULTS_DIR / "llm_cache" / "profile", cache_profile=args.cache_profile)
    print(f"=== 자격요건 평가 샘플 {len(samples)}건 (실제 AI: {settings.model}, 오늘 사용 {before}/{settings.rpd}) ===")
    rows, results, stopped = run_all(samples, profile, gates, postings, client, client, allow_external_profile=True)
    m = report(rows, results, gates, args.verbose)
    after = usage.calls_today(settings.model)
    print(f"이번 실행 호출 {after - before}회, 오늘 누적 {after}/{settings.rpd} (모델 {settings.model}). 결제 연결 여부는 코드가 확인할 수 없다: AI Studio에서 확인하라.")
    if stopped:
        print("한도 소진으로 중간에 멈췄다. 태평양 시간 자정 이후에 이어서 하라.", file=sys.stderr)
        return 3
    return 0 if m["failed"] == 0 else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ai", action="store_true", help="실제 Gemini로 실행 (가상 프로필과 샘플 공고만)")
    p.add_argument("--accept-free-tier-terms", action="store_true", help="무료 티어 입력이 제품 개선에 쓰일 수 있음에 동의")
    p.add_argument("--cache-profile", action="store_true", help="근거 찾기 결과를 디스크에 캐시한다(가상 프로필 개발용)")
    p.add_argument("--real-profile", action="store_true", help="구현하지 않음")
    p.add_argument("--model", help=".env의 GEMINI_MODEL을 덮어쓴다")
    p.add_argument("--rpm", type=int, help=".env의 GEMINI_RPM을 덮어쓴다")
    p.add_argument("--rpd", type=int, help=".env의 GEMINI_RPD를 덮어쓴다")
    p.add_argument("--postings", default=str(POSTINGS_JSON), help="공고 원문 JSON 경로")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.ai or args.real_profile:
        sys.exit(run_ai(args))
    try:
        metrics, _ = run_mock(args.verbose)
    except FileNotFoundError as e:
        print(f"정답 파일이 없다: {e}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if metrics["verdict_ok"] == metrics["samples"] else 1)


if __name__ == "__main__":
    main()
