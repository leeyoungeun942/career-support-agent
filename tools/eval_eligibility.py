"""자격요건 평가 샘플 6건 실행 (docs/policies/eligibility.md 13-1, 17장, 18장).

사용법 (저장소 루트에서):
    python -m tools.eval_eligibility                 # mock (기본): fixture 정답을 돌려준다. 네트워크를 쓰지 않는다
    python -m tools.eval_eligibility -v              # 요건별 짝과 차이를 모두 보여 준다
    python -m tools.eval_eligibility --ai --accept-free-tier-terms
                                                     # 실제 Gemini. 사용자가 직접 실행한다 (호출 12회, 가상 프로필과 샘플 공고만)
    python -m tools.eval_eligibility --ai --accept-free-tier-terms --model gemini-3.5-flash-lite --rpm 15 --rpd 500 --fresh-cache

mock의 의미: 정답을 그대로 돌려주므로 통과해도 LLM 품질은 보장하지 않는다. 파이프라인 연결과 코드 검증만 확인한다 (정책 18장).
--ai는 .env의 GEMINI_API_KEY, GEMINI_MODEL, GEMINI_RPM, GEMINI_RPD를 읽는다(--model, --rpm, --rpd로 덮어쓸 수 있다).
--fresh-cache는 이번 실행만의 공고 추출 캐시를 써서 앞 실행의 결과를 재사용하지 않는다(모델 비교 실행에 쓴다, 정책 18장).
공고 원문은 data/sample-jobs/eligibility_sample_postings.json에서 읽는다(커밋하지 않는 파일). 형식:
    {"postings": {"sample-1": {"text": "공고 원문 ..."}, ..., "sample-6": {...}}}
프로필은 fixture의 가상 프로필(김하늘)만 쓴다. 다른 프로필 입력과 --real-profile은 구현하지 않았다 (정책 16장).
출력은 공고 id, 요건 id, 판정·사유 코드, 숫자뿐이다(-v에서도 같다). 요건 text, 공고 원문, 프로필 인용, 모델 출력과 오류 내용은 출력도 저장도 하지 않는다.
디스크에는 사용량 기록(숫자)과 공고 추출 캐시만 남는다.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from core.llm_client import MAX_RETRIES, GeminiClient, LLMQuotaExceeded, LLMStageError, format_timing, load_env
from core.llm_mock import make_evidence_mock, make_extraction_mock, synthetic_posting
from core.pipeline import assess_eligibility
from core.rules import ProfileEvidenceError, rank_jobs
from core.schemas import DISPLAY_LABELS, EligibilityProfile, JobGateResult
from tools.ai_common import AiSession, AiSetupError, prepare_session

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
SAMPLE_FIXTURE = FIXTURES / "eligibility_sample_expected.json"
EVIDENCE_FIXTURE = FIXTURES / "eligibility_evidence_mock.json"
GATE_FIXTURE = FIXTURES / "job_gate_sample_expected.json"
POSTINGS_JSON = REPO / "data" / "sample-jobs" / "eligibility_sample_postings.json"
RESULTS_DIR = REPO / "data" / "results"  # 사용량 기록과 공고 추출 캐시(커밋하지 않음)
MATCH_THRESHOLD = 0.45  # 정답 요건 요약문의 두 글자 조각이 뽑은 요건 text에 이만큼 들어 있어야 한 쌍으로 본다

FREE_TIER_NOTICE = (
    "무료 티어로 보낸 입력은 공급자가 제품 개선에 쓰고 사람이 읽을 수 있다. 이 실행은 가상 프로필(김하늘)과 샘플 공고만 보낸다. "
    "실제 지원자 정보는 보내지 않는다 (정책 16장)."
)
STAT_KEYS = ("extracted", "retained", "dropped_text_not_found", "dropped_validation_failed", "unknown_issue_count", "corrected_by_code")


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


# ---------------------------------------------------------------- 정답과 짝짓기 (정책 18장)

def _bigrams(text: str) -> set[str]:
    squeezed = re.sub(r"[\W_]+", "", text)
    return {squeezed[i:i + 2] for i in range(len(squeezed) - 1)} or {squeezed}


def match_score(want: dict, got) -> float:
    """정답 요건 요약문이 뽑은 요건 text에 얼마나 들어 있는가(0~1). 같은 종류의 숫자 비교 요건이면 가산한다."""
    a, b = _bigrams(want["text"]), _bigrams(got.text)
    contained = len(a & b) / len(a) if a else 0.0
    dice = 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0
    bonus = 0.3 if want["type"] == "numeric" and got.type == "numeric" and want.get("numeric_kind") == got.numeric_kind else 0.0
    return contained + 0.01 * dice + bonus


def match_requirements(expected: list[dict], got: list) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """정답 필수 요건과 뽑은 필수 요건을 일대일로 짝짓는다. 점수가 높은 쌍부터 정하고 기준 미만은 짝짓지 않는다.

    (짝 목록 [(정답 위치, 뽑은 위치)], 짝 없는 정답 위치, 짝 없는 뽑은 위치)를 돌려준다. 순서에 의존하지 않는다.
    """
    scored = sorted(((match_score(w, g), i, j) for i, w in enumerate(expected) for j, g in enumerate(got)), reverse=True)
    pairs, used_w, used_g = [], set(), set()
    for score, i, j in scored:
        if score >= MATCH_THRESHOLD and i not in used_w and j not in used_g:
            pairs.append((i, j))
            used_w.add(i)
            used_g.add(j)
    return (sorted(pairs), [i for i in range(len(expected)) if i not in used_w], [j for j in range(len(got)) if j not in used_g])


FIELD_CHECKS = ("type", "numeric_kind", "min_value", "max_value", "career_family", "min_degree")


def field_diffs(want: dict, got) -> tuple[list[str], int]:
    """짝지은 요건의 필드별 차이. (어긋난 칸 이름, 비교한 칸 수). 정답이 값을 정해 둔 칸만 비교한다(type은 항상)."""
    diffs, total = [], 0
    for name in FIELD_CHECKS:
        if name != "type" and name not in want and not (name == "min_value" and want.get("numeric_kind") == "career_years"):
            continue
        total += 1
        if getattr(got, name) != want.get(name):
            diffs.append(name)
    return diffs, total


def _signature(items):
    return [(r["level"], r["type"], r.get("numeric_kind")) if isinstance(r, dict) else (r.level, r.type, r.numeric_kind) for r in items]


def compare_sample(sample, requirements, result):
    """기대(fixture)와 결과를 필수(level=required) 요건끼리 비교한다. 정책 17장 표는 필수 요건 기준이다.

    1) 요건 text의 겹침으로 짝을 짓고(match_requirements), 짝지은 요건만 필드·상태·사유를 비교한다. 짝 없는 요건은 따로 센다.
    2) 위치 순서 비교는 보조 지표(positional_*)로만 남긴다. 순서가 바뀌면 엉뚱한 요건끼리 비교되므로 주 지표로 쓰지 않는다.
    우대사항은 모델이 요건으로 뽑는 것이 정상이므로 비교에서 빼고 개수만 센다.
    """
    expected = [r for r in sample["requirements"] if r["level"] == "required"]
    got_at = [i for i, r in enumerate(requirements) if r.level == "required"]
    got_req = [requirements[i] for i in got_at]
    pairs, only_want, only_got = match_requirements(expected, got_req)

    rows, field_ok, field_total, id_of = [], 0, 0, {}
    for i, j in pairs:
        want, got, res = expected[i], got_req[j], result.requirement_results[got_at[j]]
        diffs, total = field_diffs(want, got)
        field_ok, field_total = field_ok + total - len(diffs), field_total + total
        rows.append({"want": want, "got_id": got.id, "diffs": diffs, "result": res})
        id_of[want["id"]] = got.id
    want_decisive = sample["expected"]["decisive"]
    decisive_ok = all(d in id_of for d in want_decisive) and {id_of[d] for d in want_decisive} == set(result.decisive_requirement_ids)

    count_ok = len(expected) == len(got_req)
    sig_ok = count_ok and _signature(expected) == _signature(got_req)
    positional = [(w, result.requirement_results[g]) for w, g in zip(expected, got_at)] if count_ok else []
    return {
        "rows": rows, "only_want": [expected[i]["id"] for i in only_want], "only_got": [got_req[j].id for j in only_got],
        "matched": len(pairs), "field_ok": field_ok, "field_total": field_total, "decisive_ok": decisive_ok,
        "verdict_ok": result.verdict == sample["expected"]["verdict"],
        "expected_required": len(expected), "got_required": len(got_req),
        "extra_other": len(requirements) - len(got_req), "expected_other": len(sample["requirements"]) - len(expected),
        "count_ok": count_ok, "sig_ok": sig_ok, "positional_rows": positional,
    }


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


# ---------------------------------------------------------------- 실행과 보고

def run_all(samples, profile, gates, postings, extract_client, evidence_client, allow_external_profile=False):
    """샘플을 차례로 평가해 (행 목록, 결과 목록, 중단 사유)를 돌려준다. 단계 실패는 그 공고만 실패로 기록하고 이어간다.

    실패 이유는 정해진 범주 이름으로만 적는다. 예외 메시지에는 모델 출력이나 프로필 인용이 들어 있을 수 있어 옮기지 않는다.
    """
    rows, results, stopped = [], [], None
    for s in samples:
        gate = gates[s["job_id"]]
        stats: dict = {}
        row = {"sample": s, "error": None, "cmp": None, "result": None, "stats": stats}
        try:
            reqs, result = assess_eligibility(postings[s["job_id"]], gate, profile, extract_client, evidence_client,
                                              allow_external_profile, stats=stats)
            row["result"], row["cmp"] = result, compare_sample(s, reqs, result)
            results.append(result)
        except LLMQuotaExceeded:
            row["error"], stopped = "한도 소진", True
            rows.append(row)
            break
        except LLMStageError:
            row["error"] = "단계 실패(통신 또는 스키마)"
        except ValueError as e:
            row["error"] = "근거 검증 실패" if isinstance(e, ProfileEvidenceError) else "결과 조립 실패"
        rows.append(row)
    return rows, results, stopped


def report(rows, results, gates, verbose=False):
    """요약을 출력하고 지표 dict를 돌려준다. 공고 id·요건 id·판정·사유 코드·숫자만 출력한다."""
    n = len(rows)
    ok_rows = [r for r in rows if r["result"] is not None]
    keys = ("count_ok", "sig_ok", "verdict_ok", "decisive_ok", "matched", "field_ok", "field_total")
    m = {"samples": n, "completed": len(ok_rows), "failed": n - len(ok_rows), "status_ok": 0, "status_total": 0,
         "reason_ok": 0, "reason_total": 0, "unmatched_expected": 0, "unmatched_extracted": 0, "expected_required": 0,
         "positional_status_ok": 0, "positional_status_total": 0,
         **{k: sum(int(r["cmp"][k]) for r in ok_rows) for k in keys}}
    stats_total = {k: sum(r["stats"].get(k, 0) for r in rows) for k in STAT_KEYS}
    for r in rows:
        s = r["sample"]
        if r["error"]:
            print(f"[{s['job_id']}] 실패: {r['error']}")
            continue
        c, res = r["cmp"], r["result"]
        m["unmatched_expected"] += len(c["only_want"])
        m["unmatched_extracted"] += len(c["only_got"])
        m["expected_required"] += c["expected_required"]
        print(f"[{s['job_id']}] 종합 {_label(res.verdict)} (기대 {_label(s['expected']['verdict'])}) "
              f"| 필수 요건 {c['got_required']}개(기대 {c['expected_required']}): 짝 {c['matched']}, 정답에만 {len(c['only_want'])}, "
              f"뽑은 것에만 {len(c['only_got'])} | 우대·기타 {c['extra_other']}개(기대 {c['expected_other']}) | {_label(res.extraction_status)}")
        for pair in c["rows"]:
            want, got = pair["want"], pair["result"]
            m["status_total"] += 1
            same = got.status == expected_status(want)
            m["status_ok"] += same
            if want["type"] in ("skill", "qualitative") and "item_statuses" not in want:
                m["reason_total"] += 1
                m["reason_ok"] += got.reason_code == want.get("reason_code")
            if verbose or not same or pair["diffs"]:
                print(f"   {'O' if same and not pair['diffs'] else 'X'} {want['id']}←{pair['got_id']} | 상태 {_label(expected_status(want))} → {_label(got.status)}"
                      f"{' / ' + _label(got.reason_code) if got.reason_code else ''}"
                      f"{' | 어긋난 칸 ' + ','.join(pair['diffs']) if pair['diffs'] else ''}")
        if c["only_want"] or c["only_got"]:
            print(f"   - 짝 없음: 정답에만 {c['only_want'] or '-'}, 뽑은 것에만 {c['only_got'] or '-'}")
        for want, got in c["positional_rows"]:  # 보조 지표
            m["positional_status_total"] += 1
            m["positional_status_ok"] += got.status == expected_status(want)
        if verbose and r["stats"]:
            print("   추출 숫자: " + ", ".join(f"{k}={r['stats'].get(k, 0)}" for k in STAT_KEYS))
    ranked = [r.job_id for r in rank_jobs(results, [gates[r.job_id] for r in results])] if results else []
    print(f"\n정렬 결과: {ranked}")
    print(f"완료 {m['completed']}/{n}, 종합 판정 일치 {m['verdict_ok']}/{m['completed']}, 결정적 요건 일치 {m['decisive_ok']}/{m['completed']}")
    print(f"필수 요건 짝지음 {m['matched']}/{m['expected_required']} (정답에만 {m['unmatched_expected']}, 뽑은 것에만 {m['unmatched_extracted']}), "
          f"필드 일치 {m['field_ok']}/{m['field_total']}")
    print(f"짝지은 요건의 상태 일치 {m['status_ok']}/{m['status_total']}, skill·qualitative 사유 일치 {m['reason_ok']}/{m['reason_total']}")
    print(f"보조(위치 순서): 필수 요건 수 일치 {m['count_ok']}/{m['completed']}, 수·유형 일치 {m['sig_ok']}/{m['completed']}, "
          f"상태 일치 {m['positional_status_ok']}/{m['positional_status_total']}")
    print("요건 추출 숫자(합계): " + ", ".join(f"{k}={v}" for k, v in stats_total.items()))
    m["ranked"], m["extraction"] = ranked, stats_total
    return m


def run_mock(verbose=False):
    samples, profile, evidence, gates = load_fixtures()
    postings = {s["job_id"]: synthetic_posting(s) for s in samples}
    extract = make_extraction_mock(samples)
    judge = make_evidence_mock(samples, evidence)
    print("=== 자격요건 평가 샘플 6건 (mock: 정답을 돌려준다. LLM 품질은 보장하지 않는다) ===")
    rows, results, _ = run_all(samples, profile, gates, postings, extract, judge)
    return report(rows, results, gates, verbose), rows


def prepare_ai(args, job_ids: list[str], results_dir: Path | None = None, *, needed_calls: int | None = None, notice: str | None = None) -> AiSession:
    """--ai 실행의 공통 준비: 안내, 동의, 설정, 공고 원문 파일, 하루 호출 예산(공고당 요건 추출 1 + 근거 찾기 1, 각각 재시도를 포함한 최대 시도 수)을 점검한다.

    하나라도 안 되면 아무것도 보내지 않고 AiSetupError를 낸다. 이 도구와 tools.run_demo가 함께 쓴다. 공통 부분은 tools.ai_common에 있다.
    needed_calls와 notice를 주면 점검할 호출 수와 안내 문구를 바꾼다(게이트 AI를 함께 돌리는 run_demo --gate ai가 두 단계의 최대 시도 수를 합쳐 넘긴다).
    """
    if args.real_profile:
        raise AiSetupError("--real-profile은 구현하지 않았다. 실제 지원자 프로필은 팀의 별도 승인 뒤에도 이 도구로 보내지 않는다 (정책 16장).")
    loaded: dict[str, str] = {}

    def load_inputs():
        try:
            loaded.update(load_postings(Path(args.postings)))
        except FileNotFoundError as e:
            raise AiSetupError(f"실행할 수 없다: {e}") from None
        if missing := [i for i in job_ids if i not in loaded]:
            raise AiSetupError(f"공고 원문이 없는 표본: {missing}")

    session = prepare_session(
        args, notice=notice or FREE_TIER_NOTICE, needed_calls=2 * len(job_ids) * (1 + MAX_RETRIES) if needed_calls is None else needed_calls, results_dir=results_dir or RESULTS_DIR,
        cache_dir_name="extract", env_loader=load_env, client_cls=GeminiClient, before_client=load_inputs,
    )
    session.postings = loaded
    return session


def run_ai(args):
    """실제 Gemini 실행. 가상 프로필과 샘플 공고만 보낸다. 호출 전에 설정, 공고 파일, 하루 예산을 모두 점검한다."""
    samples, profile, _, gates = load_fixtures()
    try:
        session = prepare_ai(args, [s["job_id"] for s in samples])
    except AiSetupError as e:
        print(e, file=sys.stderr)
        return e.code
    settings, usage, gemini, client = session.settings, session.usage, session.gemini, session.client
    print(f"=== 자격요건 평가 샘플 {len(samples)}건 (실제 AI: {settings.model}, 실행 {usage.run_id}, 생성 설정 {gemini.config_id}, "
          f"오늘 사용 {session.calls_before}/{settings.rpd}{', 새 캐시' if args.fresh_cache else ''}) ===")
    rows, results, stopped = run_all(samples, profile, gates, session.postings, client, client, allow_external_profile=True)
    m = report(rows, results, gates, args.verbose)
    t, after = usage.run_totals(), usage.calls_today(settings.model)
    print(f"이번 실행({usage.run_id}) 호출 {t['calls']}회(실패 {t['failed']}), 캐시 적중 {t['cache_hits']}, 토큰 입력 {t['input_tokens']} / 출력 {t['output_tokens']}. "
          f"오늘 누적 {after}/{settings.rpd} (모델 {settings.model}). 결제 연결 여부는 코드가 확인할 수 없다: AI Studio에서 확인하라.")
    print(f"응답 시간(모델 호출만, 한도 대기 제외): {format_timing(usage.timing())}.")
    if stopped:
        print("한도 소진으로 중간에 멈췄다. 태평양 시간 자정 이후에 이어서 하라.", file=sys.stderr)
        return 3
    return 0 if m["failed"] == 0 else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ai", action="store_true", help="실제 Gemini로 실행 (가상 프로필과 샘플 공고만)")
    p.add_argument("--accept-free-tier-terms", action="store_true", help="무료 티어 입력이 제품 개선에 쓰일 수 있음에 동의")
    p.add_argument("--cache-profile", action="store_true", help="근거 찾기 결과를 디스크에 캐시한다(가상 프로필 개발용)")
    p.add_argument("--fresh-cache", action="store_true", help="이번 실행만의 공고 추출 캐시를 쓴다(모델 비교용)")
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
