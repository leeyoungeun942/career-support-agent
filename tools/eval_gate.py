"""원티드 공고 게이트 평가 (docs/policies/job-gate.md 6-3 검증 지표).

사용법 (저장소 루트에서):
    python -m tools.eval_gate --set dev            # mock (기본): AI 자리에 정답 항목 라벨을 넣는다
    python -m tools.eval_gate --set dev -v         # 틀린 공고의 항목별 차이와 근거를 보여 준다
    python -m tools.eval_gate --set holdout        # 확인셋: 개발셋 튜닝이 끝난 뒤 한 번만 돌린다 (6-4: 다시 재지 않는다)
    python -m tools.eval_gate --set sample         # 샘플 6건 (mock). 항목 라벨·핵심 직무군·판정·사유·경고를 정책 7-1 기대와 비교한다
    python -m tools.eval_gate --set sample --ai --accept-free-tier-terms [--fresh-cache] [--model ...]
                                                   # 샘플 6건을 실제 Gemini로 (mock 예상 논리 호출 20회, 최대 21회, 공고 정보만 보낸다, 정책 6-5)

mock의 의미: AI가 항상 정답 항목 라벨을 고른다고 가정한 상한이다 (core/job_gate_mock.py). 그래서 틀린 공고는 규칙이 만든 오류다.
단, AI가 고를 수 있는 라벨 목록(후보 직무군 + 공통·범위외·혼합) 안에 정답이 없으면 mock도 정답을 낼 수 없어 혼합을 내고 따로 센다(후보 탐색 누락).
채점 대상은 review_status=approved이고 보류가 아닌 정답뿐이다.

데이터: data/raw/eval_cases.json (공고 원문과 정답이라 커밋하지 않는다). 정답 파일은 판정·라벨이 한글이라 읽을 때 코드값으로 바꾼다.
원본은 이전 작업 폴더의 eval_gate.py다.

실제 AI(--ai)는 샘플 6건(--set sample)에서만 동작한다. 개발셋·확인셋의 실제 AI 평가는 후속 이슈라 다른 셋과 함께 주면 거부한다 (정책 8장, 6-5).
샘플 데이터: data/sample-jobs/gate_sample_cases.json(공고 원문, 커밋하지 않는 파일)과 tests/fixtures/job_gate_sample_expected.json(기대 판정).
기본은 mock이고 네트워크를 쓰지 않는다. 실행 전에 최대 논리 호출 수(항목 단위 호출 가능성 포함)로 계산한 최대 시도 수(재시도 포함)를 남은 하루 한도와 비교해 모자라면 아무것도 보내지 않는다.
화면에는 공고 id, 항목 번호, 라벨·판정·사유 코드와 숫자만 낸다. 모델이 쓴 이유와 오류 내용은 출력하지 않는다.
"""
import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path

from core import job_gate as gate
from core.job_gate_ai import make_llm_judge
from core.job_gate_mock import make_label_mock
from core.llm_client import MAX_RETRIES, GeminiClient, LLMQuotaExceeded, LLMStageError, load_env
from core.schemas import DISPLAY_LABELS
from tools.ai_common import AiSetupError, prepare_session

REPO = Path(__file__).resolve().parents[1]
CASES_JSON = REPO / "data" / "raw" / "eval_cases.json"
DEFAULT_SET = "dev"  # 확인셋(holdout)은 기본으로 돌리지 않는다 (6-4)
SAMPLE_CASES_JSON = REPO / "data" / "sample-jobs" / "gate_sample_cases.json"
SAMPLE_EXPECTED_JSON = REPO / "tests" / "fixtures" / "job_gate_sample_expected.json"
SAMPLE_IDS = tuple(f"sample-{n}" for n in range(1, 7))  # 실제 AI를 돌릴 수 있는 공고. 개발셋·확인셋 크기의 입력은 함수 경계에서도 거부한다 (정책 8장)
RESULTS_DIR = REPO / "data" / "results"  # 사용량 기록과 게이트 캐시(커밋하지 않음)
SAMPLE_NOTICE = (
    "무료 티어로 보낸 입력은 공급자가 제품 개선에 쓰고 사람이 읽을 수 있다. 이 실행은 샘플 공고의 업무 문장만 보낸다. "
    "지원자 정보는 보내지 않는다 (정책 16장)."
)
SAME = {"match", "adjacent"}            # 사용자에게 정상 평가로 가는 판정
LOWER = {"mismatch", "out_of_scope"}    # 아래 그룹으로 내려가는 판정

# 정답 파일(eval_cases.json)의 한글 표기 -> 코드값. 데이터 파일은 그대로 두고 읽을 때 바꾼다
RAW_GATE = {"일치": "match", "인접": "adjacent", "불일치": "mismatch", "검토 필요": "review_needed", "범위 외": "out_of_scope"}
RAW_REASON = {"정보 부족": "insufficient_info", "핵심 업무 없음": "no_core_tasks", "혼합 직무": "mixed_job", "직무명-업무 충돌": "title_task_conflict"}
RAW_WARNING = {"직무명 차이": "title_differs", "대분류 차이": "category_differs", "미확정 분류 사용": "unconfirmed_mapping"}
RAW_LABEL = {"공통": gate.COMMON, "범위외": gate.OUT_OF_SCOPE, "혼합": gate.MIXED}


def convert_answer(answer):
    """정답 하나의 한글 표기를 코드값으로 바꾼 복사본."""
    a = copy.deepcopy(answer)
    a["gate"] = RAW_GATE[a["gate"]]
    if a.get("reason_code"):
        a["reason_code"] = RAW_REASON[a["reason_code"]]
    a["warnings"] = [RAW_WARNING[w] for w in a.get("warnings") or []]
    for x in a["item_labels"]:
        x["label"] = RAW_LABEL.get(x["label"], x["label"])
    return a


def gradable(case):
    """채점 대상: 정답이 있고, 보류가 아니고, review_status가 approved다."""
    answer = case["answer"]
    return bool(answer) and not answer.get("hold") and answer.get("review_status") == "approved"


def load(set_name, path=None):
    data = json.loads(Path(path or CASES_JSON).read_text(encoding="utf-8"))
    selected = [c for c in data["cases"] if set_name in ("all", c["set"])]
    skipped = [c for c in selected if not gradable(c)]
    cases = [{**c, "answer": convert_answer(c["answer"])} for c in selected if gradable(c)]
    for c in cases:  # 정답이 항목마다 하나씩 있어야 한다. 어긋나면 나중에 IndexError 대신 어느 공고인지 알려 준다
        kept = sum(1 for it in c["items"] if not it.get("exclude"))
        if len(c["answer"]["item_labels"]) != kept:
            raise ValueError(f"정답 파일 오류: 공고 {c['id']}의 item_labels {len(c['answer']['item_labels'])}개, 업무 항목 {kept}개")
    return data["desired_family"], cases, skipped


def run(case, desired, ai_judge, dic):
    posting = {"job_title": case["job_title"], "position": case["position"], "items": case["items"]}
    return gate.judge(posting, desired, ai_judge, dic)


def pct(n, d):
    return f"{n}/{d} ({n / d:.0%})" if d else "-"


def compute_metrics(rows):
    """(case, record) 목록에서 6-3 지표를 센다. 각 값은 (분자, 분모) 또는 개수다."""
    ans = lambda c: c["answer"]  # noqa: E731
    same_ans = [(c, r) for c, r in rows if ans(c)["gate"] in SAME]
    diff_ans = [(c, r) for c, r in rows if ans(c)["gate"] in LOWER]
    subtasks = [s for _, r in rows for it in r["items"] for s in it["subtasks"]]
    item_total = item_ok = 0
    rule_conflicts = []
    for c, r in rows:
        expected = [x["label"] for x in ans(c)["item_labels"]]
        for it in r["items"]:
            item_total += 1
            item_ok += it["label"] == expected[it["index"]]
            for s in it["subtasks"]:
                if s["source"] == "rule" and s["label"] != expected[it["index"]] and it["label"] != expected[it["index"]]:
                    rule_conflicts.append((c["id"], it["index"] + 1, s["label"], expected[it["index"]], s["text"][:50]))
    wrong = [(c, r) for c, r in rows if r["result"] != ans(c)["gate"] or r["core_family"] != ans(c)["core_family"]
             or any(it["label"] != ans(c)["item_labels"][it["index"]]["label"] for it in r["items"])]
    return {
        "n": len(rows),
        "gate_ok": (sum(r["result"] == ans(c)["gate"] for c, r in rows), len(rows)),
        "false_mismatch": (sum(r["result"] == "mismatch" for _, r in same_ans), len(same_ans)),
        "false_out_of_scope": (sum(r["result"] == "out_of_scope" for _, r in same_ans), len(same_ans)),
        "false_match": (sum(r["result"] in SAME for _, r in diff_ans), len(diff_ans)),
        "review_needed": (sum(r["result"] == "review_needed" for _, r in rows), len(rows)),
        "review_needed_expected": sum(ans(c)["gate"] == "review_needed" for c, _ in rows),
        "core_ok": (sum(r["core_family"] == ans(c)["core_family"] for c, r in rows), len(rows)),
        "item_ok": (item_ok, item_total),
        "ai_calls": (sum(s["source"] == "ai" for s in subtasks), len(subtasks)),
        "confusion": Counter((ans(c)["gate"], r["result"]) for c, r in rows),
        "rule_conflicts": rule_conflicts,
        "wrong": wrong,
    }


def show(label):
    """사람이 읽는 이름: 직무군은 이름, 그 밖의 코드는 한글 표시."""
    return gate.cfg.JOB_FAMILIES.get(label, {}).get("name") or DISPLAY_LABELS.get(label, label)


def evaluate(set_name, use_ai=False, verbose=False, path=None):
    """mock으로 세트를 돌려 지표를 출력하고 (지표, 행, AI 라벨 목록 밖 사건)을 돌려준다."""
    if use_ai:
        raise NotImplementedError("개발셋·확인셋의 실제 AI 평가는 이번 범위 밖이다 (job-gate.md 8장, 6-5). mock으로 돌린다.")
    desired, cases, skipped = load(set_name, path)
    dic = gate.Dictionary()
    rows, events = [], []
    for c in cases:
        mock = make_label_mock([x["label"] for x in c["answer"]["item_labels"]], events, c["id"])
        rows.append((c, run(c, desired, mock, dic)))
    m = compute_metrics(rows)
    ans = lambda c: c["answer"]  # noqa: E731

    print(f"\n=== {set_name} (mock) | 설정값 {gate.cfg.CONFIG_VERSION} | 기준값 {gate.GATE_THRESHOLDS} ===")
    if skipped:
        print(f"채점 제외 {len(skipped)}건 (미확정·보류)")
    print(f"기준 판정과의 일치율      {pct(*m['gate_ok'])}")
    print(f"거짓 불일치 (일치·인접→불일치)  {pct(*m['false_mismatch'])}   <- 가장 중요")
    print(f"거짓 범위 외 (일치·인접→범위 외) {pct(*m['false_out_of_scope'])}")
    print(f"거짓 일치 (불일치·범위 외→일치·인접) {pct(*m['false_match'])}")
    print(f"검토 필요 비율            {pct(*m['review_needed'])}   (정답 {m['review_needed_expected']}건)")
    print(f"핵심 직무군 일치율        {pct(*m['core_ok'])}")
    print(f"항목 라벨 일치율          {pct(*m['item_ok'])}")
    print(f"AI 호출 비중 (하위 업무)  {pct(*m['ai_calls'])}")
    print(f"규칙 확정이 항목을 틀리게 한 하위 업무  {len(m['rule_conflicts'])}건")
    print(f"정답이 AI 라벨 목록 밖     {len(events)}건 (후보 탐색 누락)")
    print("판정 분포 (정답 → 결과):")
    for (a, p), k in sorted(m["confusion"].items(), key=lambda x: (x[0][0] != x[0][1], show(x[0][0]), show(x[0][1]))):  # 한글 표시 순서
        print(f"   {show(a):6s} → {show(p):6s} {k}건{'' if a == p else '   <-- 다름'}")

    if m["wrong"]:
        print(f"\n--- 정답과 다른 공고 {len(m['wrong'])}건 ---")
    for c, r in m["wrong"]:
        a = ans(c)
        reason = ", " + DISPLAY_LABELS[r["reason_code"]] if r["reason_code"] else ""
        warnings = ", 경고 " + str([DISPLAY_LABELS[w] for w in r["warnings"]]) if r["warnings"] else ""
        print(f"\n[{c['id']}] {c['job_title'][:40]} | 정답 {show(a['gate'])}/{show(a['core_family']) if a['core_family'] else '-'}"
              f" → 결과 {show(r['result'])}/{show(r['core_family']) if r['core_family'] else '-'} (끝난 단계 {r['ended_at']}{reason}{warnings})")
        for it in r["items"]:
            want = a["item_labels"][it["index"]]["label"]
            if it["label"] != want or verbose:
                print(f"   {'X' if it['label'] != want else 'O'} 항목 {it['index'] + 1} {show(it['label'])}"
                      f" (정답 {show(want)}): {(it['heading'] or it['text'])[:48]}")
                if it["label"] != want:
                    for s in it["subtasks"]:
                        cues = ' 단서=' + str({k.replace('JF_', ''): v[:2] for k, v in s['evidence'].items()}) if s["evidence"] else ""
                        print(f"        [{s['source']}] {show(s['label'])}: {s['text'][:60]}{cues}")
        if not r["items"]:
            print(f"   (항목 라벨링 전에 끝남: {r['ended_at']}단계)")
        if a.get("note") and verbose:
            print(f"   정답 근거: {a['note'][:150]}")
    for e in events:
        print(f"\n  ! AI 라벨 목록 밖: [{e['case']}] 항목 {e['item']} 정답 {show(e['want'])}, 허용 {e['allowed']} / {e['text']}")
    for cid, item, got, want, text in m["rule_conflicts"]:
        print(f"  ? 규칙 확정 충돌: [{cid}] 항목 {item} {show(got)} (정답 {show(want)}): {text}")
    return m, rows, events


# ---------------------------------------------------------------- 샘플 6건 (정책 7-1, 6-5)

CATEGORY_AI = "AI 판단 오류"
CATEGORY_RULE = "규칙 확정 오류"
CATEGORY_CANDIDATE = "후보 탐색 누락"
CATEGORY_FAILED = "호출 실패"
CATEGORIES = (CATEGORY_AI, CATEGORY_RULE, CATEGORY_CANDIDATE, CATEGORY_FAILED)  # 정답 불확실은 사람이 정한다


def load_sample(cases_path=None, expected_path=None):
    """(희망 직무군, 샘플 공고 목록). 원문 파일과 기대 판정 fixture를 번호로 짝짓는다. 어긋나면 ValueError, 원문 파일이 없으면 FileNotFoundError."""
    cases_path, expected_path = Path(cases_path or SAMPLE_CASES_JSON), Path(expected_path or SAMPLE_EXPECTED_JSON)
    if not cases_path.exists():
        raise FileNotFoundError(f"샘플 공고 원문 파일이 없다: {cases_path} (팀 Drive에서 받아 넣는다. 커밋하지 않는 파일)")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    raw = {c["no"]: c for c in json.loads(cases_path.read_text(encoding="utf-8"))["cases"]}
    cases = []
    for exp in expected["cases"]:
        r = raw.get(exp["no"])
        if r is None:
            raise ValueError(f"샘플 원문 파일에 {exp['no']}번 공고가 없다")
        if r["company"] != exp["company"]:
            raise ValueError(f"{exp['no']}번: 원문 파일과 기대 판정 fixture의 회사가 다르다")
        kept = [it for it in r["items"] if not it.get("exclude")]
        if len(kept) != len(exp["item_labels"]):
            raise ValueError(f"{exp['no']}번: 업무 항목 수가 기대 판정 fixture와 다르다")
        cases.append({
            "id": f"sample-{exp['no']}", "no": exp["no"], "job_title": r["job_title"], "position": r["position"], "items": r["items"],
            "expected_gate": exp["expected"]["gate"], "expected_core": exp["expected"]["core_family"],
            "expected_labels": exp["item_labels"],
        })
    return expected["desired_family"], cases


def _recording(judge, requests):
    def wrapped(request):
        requests.append(request)  # 호출을 시도한 요청. 실패해도 센다
        return judge(request)
    return wrapped


def max_logical_calls(record):
    """규칙이 정한 구조에서 실제 AI가 만들 수 있는 최대 논리 호출 수 (정책 5-2 호출 예산).

    하위 업무 호출은 규칙이 정하므로 AI 라벨과 무관하다(source=ai인 하위 업무 수). 항목 단위 호출은 '공통이 아닌 하위 업무가 정확히 절반'일 때 생기고
    그것은 AI가 고른 라벨에 달렸다. 규칙이 확정한 하위 업무(r, 항상 공통이 아님)와 AI가 부른 하위 업무(a)가 있을 때 공통이 아닌 수는 r..r+a 어디든 될 수 있으므로,
    하위 업무 수 n이 짝수이고 r <= n/2 <= r+a이면 그 항목은 항목 단위 호출이 하나 더 생길 수 있다.
    """
    total = 0
    for item in record["items"]:
        subs = item["subtasks"]
        n, ai = len(subs), sum(1 for sub in subs if sub["source"] == "ai")
        rule = n - ai
        total += ai
        if n > 0 and n % 2 == 0 and rule <= n // 2 <= rule + ai:
            total += 1
    return total


def ai_call_bounds(cases, desired, dic=None):
    """(mock 예상 논리 호출 수, 최대 논리 호출 수). mock은 정답 라벨을 돌려주므로 mock 예상은 상한이 아니다.

    실제 AI가 다른 라벨을 고르면 항목 단위 호출이 더 생길 수 있다. 최대 논리 호출 수는 같은 mock 실행의 구조(어느 하위 업무를 AI가 불렀는가)에서 계산한다.
    """
    dic = dic or gate.Dictionary()
    expected = maximum = 0
    for c in cases:
        requests = []
        record = run(c, desired, _recording(make_label_mock(c["expected_labels"]), requests), dic)
        expected += len(requests)
        maximum += max_logical_calls(record)
    return expected, maximum


def count_ai_calls(cases, desired, dic=None):
    """정답 라벨을 돌려주는 mock으로 같은 입력을 돌려 AI 호출(논리 호출) 수를 센다. 상한이 아니다(ai_call_bounds가 최대를 센다)."""
    return ai_call_bounds(cases, desired, dic)[0]


def check_ai_scope(cases):
    """실제 AI를 돌려도 되는 입력인지 함수 경계에서 확인한다. 샘플 6건(sample-1~6)만 허용한다. CLI 검사에 기대지 않는다."""
    ids = [c["id"] for c in cases]
    if not ids or len(set(ids)) != len(ids) or len(ids) > len(SAMPLE_IDS) or not set(ids) <= set(SAMPLE_IDS):
        raise AiSetupError(
            f"실제 AI는 샘플 6건({SAMPLE_IDS[0]} ~ {SAMPLE_IDS[-1]})에서만 돌린다. 이 입력은 허용 범위 밖이다({len(ids)}건). "
            "개발셋·확인셋의 실제 AI 평가는 이번 범위 밖이다 (job-gate.md 8장, 6-5). 아무것도 보내지 않았다."
        )


def _item_category(item_index, expected, requests):
    mine = [r for r in requests if r["item_index"] == item_index]
    if any(expected not in r["allowed"] for r in mine):
        return CATEGORY_CANDIDATE
    return CATEGORY_AI if mine else CATEGORY_RULE


def compare_sample(case, record, requests):
    """샘플 1건의 기대와 결과를 비교해 불일치 목록 [(범주, 설명)]을 돌려준다. 정답지는 고치지 않는다."""
    problems = []
    got = [it["label"] for it in record["items"]]
    for it in record["items"]:
        want = case["expected_labels"][it["index"]]
        if it["label"] != want:
            problems.append((_item_category(it["index"], want, requests), f"항목 {it['index'] + 1} {show(it['label'])} (기대 {show(want)})"))
    if len(got) != len(case["expected_labels"]):
        problems.append((CATEGORY_RULE, f"항목 수 {len(got)} (기대 {len(case['expected_labels'])})"))
    if not problems:  # 항목 라벨이 모두 같은데 위 단계의 결과가 다르면 항목 집계·판정 규칙의 문제다
        for what, g, w in (("판정", record["result"], case["expected_gate"]), ("핵심 직무군", record["core_family"], case["expected_core"]),
                           ("사유 코드", record["reason_code"], None), ("경고 코드", record["warnings"], [])):
            if g != w:
                problems.append((CATEGORY_RULE, f"{what} {g} (기대 {w})"))
    return problems


def run_sample(cases, desired, judge_for_case):
    """샘플을 차례로 돌린다. 한 공고의 호출 실패는 그 공고만 실패로 두고 이어가고, 한도 소진은 남은 공고를 시작하지 않고 멈춘다.

    (행 목록, 중단 여부)를 돌려준다. 행: case, record(실패면 None), requests, error(범주 이름 또는 None), problems.
    """
    dic = gate.Dictionary()
    rows, stopped = [], False
    for c in cases:
        requests = []
        row = {"case": c, "record": None, "requests": requests, "error": None, "problems": []}
        try:
            row["record"] = run(c, desired, _recording(judge_for_case(c), requests), dic)
            row["problems"] = compare_sample(c, row["record"], requests)
        except LLMQuotaExceeded:
            row["error"], stopped = "한도 소진", True
        except LLMStageError:
            row["error"] = "AI 호출 실패"
            row["problems"] = [(CATEGORY_FAILED, "AI 호출 실패")]
        rows.append(row)
        if stopped:
            break
    return rows, stopped


def report_sample(rows, total_cases, mode, verbose=False):
    """샘플 결과를 출력하고 지표 dict를 돌려준다. 공고 id·코드·숫자만 낸다."""
    done = [r for r in rows if r["record"] is not None]
    m = {"cases": total_cases, "ran": len(rows), "done": len(done), "failed": sum(1 for r in rows if r["error"]),
         "all_ok": 0, "result_ok": 0, "core_ok": 0, "items_ok": 0, "items_total": 0, "calls": 0, "categories": Counter()}
    print(f"\n=== sample ({mode}) | 설정값 {gate.cfg.CONFIG_VERSION} ===")
    for r in rows:
        c, rec = r["case"], r["record"]
        m["calls"] += len(r["requests"])
        for cat, _ in r["problems"]:
            m["categories"][cat] += 1
        if rec is None:
            print(f"[{c['id']}] 실패: {r['error']} (AI 호출 {len(r['requests'])}회 시도)")
            continue
        n_ok = sum(it["label"] == c["expected_labels"][it["index"]] for it in rec["items"])
        m["items_ok"] += n_ok
        m["items_total"] += len(c["expected_labels"])
        m["result_ok"] += rec["result"] == c["expected_gate"]
        m["core_ok"] += rec["core_family"] == c["expected_core"]
        m["all_ok"] += not r["problems"]
        print(f"[{c['id']}] 판정 {show(rec['result'])}(기대 {show(c['expected_gate'])}) | 핵심 직무군 {show(rec['core_family']) if rec['core_family'] else '-'}"
              f"(기대 {show(c['expected_core'])}) | 항목 라벨 {n_ok}/{len(c['expected_labels'])} | 사유 {rec['reason_code'] or '-'} 경고 {rec['warnings'] or '-'}"
              f" | AI 호출 {len(r['requests'])}회 | {'일치' if not r['problems'] else '불일치'}")
        for cat, text in r["problems"]:
            print(f"   X [{cat}] {text}")
        if verbose:
            for it in rec["items"]:
                print(f"   {'O' if it['label'] == c['expected_labels'][it['index']] else 'X'} 항목 {it['index'] + 1} {show(it['label'])}"
                      f" ({'AI' if any(s['source'] == 'ai' for s in it['subtasks']) else '규칙'})")
    skipped = total_cases - len(rows)
    print(f"\n공고 전체 일치 {m['all_ok']}/{total_cases} (호출이 끝난 공고만 분모: {m['all_ok']}/{m['done']})"
          f"{f', 시작하지 못한 공고 {skipped}건' if skipped else ''}")
    print(f"판정 일치 {m['result_ok']}/{total_cases} (끝난 공고만 {m['result_ok']}/{m['done']}), 핵심 직무군 일치 {m['core_ok']}/{total_cases}, "
          f"항목 라벨 일치 {m['items_ok']}/{m['items_total']}")
    print(f"AI 호출(논리) {m['calls']}회, AI 실패 {m['failed']}건")
    if m["categories"]:
        print("불일치 원인(자동 분류): " + ", ".join(f"{cat} {m['categories'][cat]}" for cat in CATEGORIES if m["categories"][cat])
              + " | 정답 불확실은 사람이 판단해 기록한다 (정책 6-5)")
    return m


def run_sample_mock(verbose=False, cases_path=None, expected_path=None):
    desired, cases = load_sample(cases_path, expected_path)
    rows, _ = run_sample(cases, desired, lambda c: make_label_mock(c["expected_labels"]))
    return report_sample(rows, len(cases), "mock: 정답 라벨을 돌려준다", verbose), rows


def run_sample_ai(args, cases_path=None, expected_path=None):
    """샘플 6건을 실제 Gemini로 돌린다. 공고 정보만 보낸다. 호출 전에 약관 동의, 설정, 하루 예산(최대 시도 수)을 모두 점검한다."""
    try:
        desired, cases = load_sample(cases_path, expected_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"실행할 수 없다: {e}", file=sys.stderr)
        return 2
    try:
        check_ai_scope(cases)
    except AiSetupError as e:
        print(e, file=sys.stderr)
        return e.code
    expected_calls, logical = ai_call_bounds(cases, desired)  # mock 예상은 상한이 아니다. 예산은 최대 논리 호출로 계산한다
    attempts = logical * (1 + MAX_RETRIES)  # 재시도 때문에 한 논리 호출이 최대 이만큼 시도된다. 실패한 시도도 한도에 센다
    notice = SAMPLE_NOTICE + f"\n예상 호출: mock 예상 논리 {expected_calls}회, 최대 논리 {logical}회, 최대 시도 {attempts}회(재시도 {MAX_RETRIES}회 포함)."
    try:
        session = prepare_session(args, notice=notice, needed_calls=attempts, results_dir=RESULTS_DIR, cache_dir_name="gate",
                                  env_loader=load_env, client_cls=GeminiClient)
    except AiSetupError as e:
        print(e, file=sys.stderr)
        return e.code
    settings, usage = session.settings, session.usage
    print(f"=== 게이트 샘플 {len(cases)}건 (실제 AI: {settings.model}, 실행 {usage.run_id}, 생성 설정 {session.gemini.config_id}, "
          f"논리 호출 예상 {expected_calls}회, 최대 논리 {logical}회, 최대 시도 {attempts}회, 오늘 이미 사용 {session.calls_before}/{settings.rpd}"
          f"{', 새 캐시' if args.fresh_cache else ''}) ===")
    rows, stopped = run_sample(cases, desired, lambda c: make_llm_judge(session.client, c["id"]))
    m = report_sample(rows, len(cases), f"AI {settings.model}", args.verbose)
    t, after = usage.run_totals(), usage.calls_today(settings.model)
    print(f"호출 비교: mock 예상 논리 {expected_calls}회 / 최대 논리 {logical}회 / 실제 논리 {m['calls']}회 / 실제 시도 {t['calls']}회(최대 시도 {attempts}회).")
    if m["calls"] > logical or t["calls"] > attempts:
        print("경고: 실제 호출이 계산한 최대치를 넘었다. 호출 예산 계산에 결함이 있다.", file=sys.stderr)
    print(f"이번 실행({usage.run_id}) 시도 {t['calls']}회(실패 {t['failed']}), 캐시 적중 {t['cache_hits']}, 토큰 입력 {t['input_tokens']} / 출력 {t['output_tokens']}. "
          f"오늘 누적 {after}/{settings.rpd} (모델 {settings.model}). 결제 연결 여부는 코드가 확인할 수 없다: AI Studio에서 확인하라.")
    if stopped:
        print("한도 소진으로 중간에 멈췄다. 태평양 시간 자정 이후에 이어서 하라.", file=sys.stderr)
        return 3
    return 0 if m["all_ok"] == len(cases) else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", choices=["sample", "dev", "holdout", "all"], default=DEFAULT_SET)
    p.add_argument("--ai", action="store_true", help="실제 AI로 샘플 6건을 실행 (--set sample에서만, 공고 정보만 보낸다)")
    p.add_argument("--accept-free-tier-terms", action="store_true", help="무료 티어 입력이 제품 개선에 쓰일 수 있음에 동의")
    p.add_argument("--fresh-cache", action="store_true", help="이번 실행만의 새 캐시를 쓴다(모델 비교·재현성 확인용)")
    p.add_argument("--model", help=".env의 GEMINI_MODEL을 덮어쓴다")
    p.add_argument("--rpm", type=int, help=".env의 GEMINI_RPM을 덮어쓴다")
    p.add_argument("--rpd", type=int, help=".env의 GEMINI_RPD를 덮어쓴다")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.ai and args.set != "sample":
        print("실제 AI는 샘플 6건(--set sample)에서만 돌린다. 개발셋·확인셋의 실제 AI 평가는 이번 범위 밖이다 (job-gate.md 8장, 6-5). 아무것도 보내지 않았다.", file=sys.stderr)
        sys.exit(2)
    if args.set == "sample":
        if args.ai:
            sys.exit(run_sample_ai(args))
        try:
            metrics, _ = run_sample_mock(args.verbose)
        except (FileNotFoundError, ValueError) as e:
            print(f"실행할 수 없다: {e}", file=sys.stderr)
            sys.exit(2)
        sys.exit(0 if metrics["all_ok"] == metrics["cases"] else 1)
    if not CASES_JSON.exists():
        print(f"정답 파일이 없다: {CASES_JSON} (팀 Drive에서 받아 data/raw/에 넣는다. 커밋하지 않는 파일)", file=sys.stderr)
        sys.exit(2)
    if args.set in ("holdout", "all"):
        print("확인셋입니다. 결과를 보고 규칙·기준값을 고치면 안 됩니다 (정책 6-4: 확인셋은 다시 재지 않는다).")
    evaluate(args.set, verbose=args.verbose)


if __name__ == "__main__":
    main()
