"""원티드 공고 게이트 평가 (docs/policies/job-gate.md 6-3 검증 지표).

사용법 (저장소 루트에서):
    python -m tools.eval_gate --set dev            # mock (기본): AI 자리에 정답 항목 라벨을 넣는다
    python -m tools.eval_gate --set dev -v         # 틀린 공고의 항목별 차이와 근거를 보여 준다
    python -m tools.eval_gate --set holdout        # 확인셋: 개발셋 튜닝이 끝난 뒤 한 번만 돌린다 (6-4: 다시 재지 않는다)
    python -m tools.eval_gate --set dev --ai       # 실제 AI: 아직 연결하지 않았다 (job-gate.md 8장 보류)

mock의 의미: AI가 항상 정답 항목 라벨을 고른다고 가정한 상한이다 (core/job_gate_mock.py). 그래서 틀린 공고는 규칙이 만든 오류다.
단, AI가 고를 수 있는 라벨 목록(후보 직무군 + 공통·범위외·혼합) 안에 정답이 없으면 mock도 정답을 낼 수 없어 혼합을 내고 따로 센다(후보 탐색 누락).
채점 대상은 review_status=approved이고 보류가 아닌 정답뿐이다.

데이터: data/raw/eval_cases.json (공고 원문과 정답이라 커밋하지 않는다). 정답 파일은 판정·라벨이 한글이라 읽을 때 코드값으로 바꾼다.
원본은 이전 작업 폴더의 eval_gate.py다.
"""
import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path

from core import job_gate as gate
from core.job_gate_mock import make_label_mock
from core.schemas import DISPLAY_LABELS

REPO = Path(__file__).resolve().parents[1]
CASES_JSON = REPO / "data" / "raw" / "eval_cases.json"
DEFAULT_SET = "dev"  # 확인셋(holdout)은 기본으로 돌리지 않는다 (6-4)
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
        raise NotImplementedError("실제 AI는 아직 연결하지 않았다 (job-gate.md 8장 보류). mock으로 돌린다.")
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


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", choices=["dev", "holdout", "all"], default=DEFAULT_SET)
    p.add_argument("--ai", action="store_true", help="실제 AI (아직 연결하지 않음, job-gate.md 8장 보류)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.ai:
        print("실제 AI는 아직 연결하지 않았다 (job-gate.md 8장 보류). --ai 없이 mock으로 돌린다.", file=sys.stderr)
        sys.exit(2)
    if not CASES_JSON.exists():
        print(f"정답 파일이 없다: {CASES_JSON} (팀 Drive에서 받아 data/raw/에 넣는다. 커밋하지 않는 파일)", file=sys.stderr)
        sys.exit(2)
    if args.set in ("holdout", "all"):
        print("확인셋입니다. 결과를 보고 규칙·기준값을 고치면 안 됩니다 (정책 6-4: 확인셋은 다시 재지 않는다).")
    evaluate(args.set, verbose=args.verbose)


if __name__ == "__main__":
    main()
