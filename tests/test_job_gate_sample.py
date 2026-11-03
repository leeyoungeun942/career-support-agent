"""직무 게이트 판정 회귀 테스트 (이슈 #5). 원본은 이전 작업 폴더의 test_sample_gate.py다.

샘플 공고 6건 테스트(job-gate.md 7-1 기대 판정)는 공고 원문이 필요하다. 원문은 커밋하지 않는 파일이라
`data/sample-jobs/gate_sample_cases.json`이 없으면 건너뛴다(팀 Drive에서 받아 넣는다). 기대 판정(원문 없음)은
`tests/fixtures/job_gate_sample_expected.json`에 있고 정책 7-1 표와 같은지 따로 확인한다.
스텁 모드: AI 자리에 7-1 기대 항목 라벨을 넣어 규칙(단서 탐색·문맥 판별·공통 단서·과반·직무명 대조·인접)만 확인한다.
단어 시작 경계, 항목 과반, 범위외 근거, 5-3 기준값 경계 테스트는 합성 공고를 써서 원문 없이도 돈다.

이 단계(Step 3)는 원본 한글 출력을 그대로 쓴다. 영문 코드로의 전환은 Step 4다.
"""
import json
import re
from collections import Counter
from pathlib import Path

import pytest

import policy_docs as pd
from core import job_gate as gate
from core import job_gate_config as cfg

RAW_CASES = Path(__file__).resolve().parents[1] / "data" / "sample-jobs" / "gate_sample_cases.json"
EXPECTED = Path(__file__).parent / "fixtures" / "job_gate_sample_expected.json"

needs_raw = pytest.mark.skipif(
    not RAW_CASES.exists(),
    reason="샘플 공고 원문이 없어 건너뜀: data/sample-jobs/gate_sample_cases.json (팀 Drive에서 받아 넣는다. 커밋하지 않는 파일)",
)


def load_expected():
    data = json.loads(EXPECTED.read_text(encoding="utf-8"))
    return data["desired_family"], data["cases"]


def load_cases():
    """원문(로컬 파일)과 기대 판정(fixture)을 번호로 짝짓는다."""
    desired, expected = load_expected()
    raw = {c["no"]: c for c in json.loads(RAW_CASES.read_text(encoding="utf-8"))["cases"]}
    cases = []
    for exp in expected:
        r = raw[exp["no"]]
        assert r["company"] == exp["company"], f"{exp['no']}번: 원문 파일과 기대 판정 fixture의 회사가 다르다"
        kept = [it for it in r["items"] if not it.get("exclude")]
        assert len(kept) == len(exp["item_labels"]), f"{exp['no']}번: 업무 항목 수가 fixture와 다르다"
        cases.append({**exp, "job_title": r["job_title"], "position": r["position"], "items": r["items"]})
    return desired, cases


def make_stub(case):
    labels = case["item_labels"]

    def stub(request):
        label = labels[request["item_index"]]
        if label not in request["allowed"]:
            # 후보에 없는 라벨이면 그대로 돌려주지 않고 드러낸다 (규칙이 후보를 잘못 좁힌 것)
            raise AssertionError(f"스텁 라벨 '{label}'이 허용 목록에 없음: {request['allowed']} / {request['text']}")
        return {"label": label, "reason": "7-1 기대 라벨 (스텁)"}

    return stub


def run_case(case, desired, ai_judge, dic):
    posting = {"job_title": case["job_title"], "position": case["position"], "items": case["items"]}
    return gate.judge(posting, desired, ai_judge, dic)


def check_case(case, record):
    """기대와 다른 점을 문자열 목록으로 돌려준다."""
    problems = []
    exp = case["expected"]
    if record["result"] != exp["gate"]:
        problems.append(f"게이트 결과 {record['result']} (기대 {exp['gate']})")
    if record["core_family"] != exp["core_family"]:
        problems.append(f"핵심 직무군 {record['core_family']} (기대 {exp['core_family']})")
    got = [it["label"] for it in record["items"]]
    for i, (g, e) in enumerate(zip(got, case["item_labels"]), start=1):
        if g != e:
            problems.append(f"항목 {i} 라벨 {g} (기대 {e})")
    if len(got) != len(case["item_labels"]):
        problems.append(f"항목 수 {len(got)} (기대 {len(case['item_labels'])})")
    if record["warnings"]:
        problems.append(f"경고 코드 {record['warnings']} (7-1: 6건 모두 경고 없음)")
    return problems


# ---------------------------------------------------------------------
# 샘플 6건 (원문 필요)
# ---------------------------------------------------------------------
@needs_raw
def test_sample_cases_stub():
    """7-1: 샘플 6건의 게이트 판정, 핵심 직무군, 항목 라벨이 기대와 같고 경고가 없다."""
    desired, cases = load_cases()
    dic = gate.Dictionary()
    assert len(cases) == 6
    for case in cases:
        record = run_case(case, desired, make_stub(case), dic)
        assert not check_case(case, record), (case["no"], check_case(case, record))


@needs_raw
def test_gate_ignores_qualification():
    """1번: 게이트는 일치. 자격요건 미충족은 게이트 입력에 없으므로 결과에 영향을 주지 않는다 (1-2)."""
    desired, cases = load_cases()
    case = cases[0]
    posting = {"job_title": case["job_title"], "position": case["position"], "items": case["items"]}
    assert set(posting) == {"job_title", "position", "items"}  # 자격요건·경력 필드가 없다
    assert gate.judge(posting, desired, make_stub(case))["result"] == "일치"


@needs_raw
def test_ai_request_carries_no_forbidden_inputs():
    """5-2: AI에 직무명·모집부문·자격요건·우대사항·다른 항목의 라벨을 주지 않는다. 요청(request)만으로 확인한다.

    원본은 프롬프트 문자열을 검사했다. 프롬프트를 만드는 AI 호출 모듈은 이번에 이식하지 않아 요청 단계에서 같은 보장을 확인한다.
    """
    desired, cases = load_cases()
    dic = gate.Dictionary()
    expected_keys = {"level", "item_index", "sub_index", "text", "item_text", "other_items", "candidates", "allowed", "families"}
    n_requests = 0
    for case in cases:
        captured = []

        def recorder(request, _labels=case["item_labels"], _sink=captured):
            _sink.append(request)
            return {"label": _labels[request["item_index"]], "reason": ""}

        run_case(case, desired, recorder, dic)
        kept_texts = [(it.get("heading", "") + " " + it["text"]).strip() for it in case["items"] if not it.get("exclude")]
        for request in captured:
            n_requests += 1
            assert set(request) == expected_keys, (case["no"], set(request) ^ expected_keys)
            serialized = json.dumps(request, ensure_ascii=False)
            for forbidden in (case["job_title"], case["position"], "자격요건", "우대사항"):
                assert forbidden not in serialized, (case["no"], forbidden)
            for other in request["other_items"]:
                assert other in kept_texts  # 나머지 항목은 원문으로만 준다 (라벨 없음)
            assert request["item_text"] == kept_texts[request["item_index"]]
            assert request["text"] in request["item_text"]
    assert n_requests > 0


# ---------------------------------------------------------------------
# 합성 공고: 원문 없이 도는 규칙 회귀
# ---------------------------------------------------------------------
def test_word_start_boundary():
    """5-2: 공백을 지워도 두 단어에 걸친 우연한 일치는 인정하지 않는다."""
    def has(term, text):
        return gate.find_term(gate.norm(term), gate.norm(text), gate.word_starts(text))

    assert not has("영업", "이해관계자와의 운영 업무 대응")       # '운영업무'의 '영업'
    assert has("경영계획", "전사 연간 경영 계획을 수립")           # 공백 제거 비교는 유지
    assert has("운영", "기획·운영 업무")                            # 가운뎃점 뒤는 새 단어
    assert has("스트리밍", "표시 방식 (스트리밍, 타이핑 효과 등)")  # 괄호 뒤는 새 단어
    assert has("서비스기획", "서비스 기획에 반영")                  # 조사가 붙은 단어 끝은 따지지 않는다
    assert not has("MD", "AMD 칩 개발")                              # 영문 단어 중간


def test_item_label_majority():
    """5-2 항목 라벨: 공통이 아닌 하위 업무에서 한 라벨이 과반이면 그 라벨, 과반이 없으면 혼합."""
    sp, pm = "JF_SERVICE_PLANNING", "JF_PROJECT_MGMT"
    never = lambda request: (_ for _ in ()).throw(AssertionError("AI를 부르면 안 된다"))
    subs = lambda *labels: [{"label": l} for l in labels]
    assert gate.item_label(subs(sp, sp, pm), "", 0, [], never, [])[0] == sp
    assert gate.item_label(subs(sp, pm), "", 0, [], never, [])[0] == gate.MIXED
    assert gate.item_label(subs(sp, "공통", "공통"), "", 0, [], never, [])[0] == "공통"  # 절반 미만


def test_out_of_scope_needs_evidence():
    """5-1 1단계 ④: 직무군 단서가 없다는 사실만으로는 범위 외로 끝내지 않는다."""
    desired = "JF_SERVICE_PLANNING"
    ai = lambda request: {"label": gate.OUT_OF_SCOPE, "reason": "stub"}
    no_evidence = {"job_title": "담당자", "position": "", "items": [
        {"text": "알 수 없는 업무를 처리합니다"}, {"text": "또 다른 알 수 없는 업무를 합니다"}]}
    r = gate.judge(no_evidence, desired, ai)
    assert r["ended_at"] != 1 and r["result"] == "범위 외"        # AI 판별을 거쳐 4단계에서 범위 외
    with_evidence = {"job_title": "담당자", "position": "", "items": [
        {"text": "마케팅 캠페인을 운영합니다"}, {"text": "광고 소재를 만듭니다"}]}
    r = gate.judge(with_evidence, desired, ai)
    assert r["ended_at"] == 1 and r["result"] == "범위 외"        # 범위외 근거가 있어 1단계에서 끝
    assert not r["ai_log"]


def test_threshold_boundaries():
    """5-3 기준값(정보 부족 2개 미만, 명확성 2/3 이상)의 경계 동작을 고정한다. 기준값을 바꾸면 이 테스트도 고친다."""
    dev, sp = "JF_DEV", "JF_SERVICE_PLANNING"

    def judge_with(labels, title="백엔드 개발자 모집"):
        items = [{"text": f"업무 {i}를 처리합니다"} for i in range(len(labels))]
        ai = lambda request: {"label": labels[request["item_index"]], "reason": "stub"}
        return gate.judge({"job_title": title, "position": "", "items": items}, sp, ai)

    # 정보 부족: 항목 1개는 검토 필요, 2개는 판정으로 간다
    r = judge_with([dev])
    assert (r["result"], r["reason_code"], r["ended_at"]) == ("검토 필요", "정보 부족", 2)
    assert judge_with([dev, dev])["result"] == "불일치"

    # 명확성: 직무명(개발자)이 가리키는 직무군과 핵심 직무군(서비스기획)이 다를 때
    r = judge_with([sp, sp, sp, dev, dev])                  # 3/5 = 0.60 < 2/3
    assert (r["result"], r["reason_code"]) == ("검토 필요", "직무명-업무 충돌") and r["core_family"] == sp
    r = judge_with([sp, sp, sp, sp, dev, dev])              # 4/6 = 2/3 (경계값 포함)
    assert r["result"] == "일치" and r["warnings"] == ["직무명 차이"]
    r = judge_with([sp, sp, sp, sp])                         # 4/4, 직무명과 다르지만 명확
    assert r["result"] == "일치" and r["warnings"] == ["직무명 차이"]


def test_gate_thresholds_are_the_policy_5_3_initial_values():
    assert gate.GATE_THRESHOLDS == {"min_items": 2, "clear_ratio": 2 / 3}


# ---------------------------------------------------------------------
# 기대 판정 fixture <-> 정책 7-1 표 (원문 없이 돈다)
# ---------------------------------------------------------------------
def policy_7_1_rows():
    text = pd.POLICY_MD.read_text(encoding="utf-8")
    rows = {}
    for line in pd.section(text, "### 7-1."):
        if not line.startswith("|"):
            continue
        cells = pd.table_cells(line)
        if cells[0].isdigit():
            rows[int(cells[0])] = cells
    return rows


@pytest.mark.policy_contract
def test_expected_fixture_matches_policy_7_1_table():
    """원문 없이 커밋하는 기대 판정이 정책 7-1 표(판정, 핵심 직무군과 비율, 항목 라벨 개수)와 같다."""
    desired, expected = load_expected()
    rows = policy_7_1_rows()
    assert desired == "JF_SERVICE_PLANNING"
    assert sorted(rows) == [c["no"] for c in expected] == [1, 2, 3, 4, 5, 6]
    to_id = {f["name"]: fid for fid, f in cfg.JOB_FAMILIES.items()}
    to_id["공통"] = "공통"
    for case in expected:
        _, company_title, labels_cell, core_cell, gate_cell, _ = policy_7_1_rows()[case["no"]]
        assert company_title.split(",")[0] in case["company"], case["no"]
        # 판정과 핵심 직무군
        assert gate_cell.strip("*") == case["expected"]["gate"], case["no"]
        m = re.fullmatch(r"(\S+) \((\d+)/(\d+)\)", core_cell)
        assert m, core_cell
        assert to_id[m.group(1)] == case["expected"]["core_family"], case["no"]
        # 항목 라벨 개수: "서비스기획 2, 공통 2 (…)" 처럼 괄호 설명은 뗀다
        counts = Counter()
        for part in re.sub(r"\([^)]*\)", "", labels_cell).split(","):
            name, n = part.split()
            counts[to_id[name]] += int(n)
        assert counts == Counter(case["item_labels"]), (case["no"], counts)
        # 핵심 비율 = 핵심 직무군 항목 수 / 공통을 뺀 항목 수
        non_common = [l for l in case["item_labels"] if l != "공통"]
        assert (non_common.count(case["expected"]["core_family"]), len(non_common)) == (int(m.group(2)), int(m.group(3)))


def test_expected_fixture_has_no_posting_text():
    """원문을 커밋하지 않는다는 완료 조건. fixture는 번호, 회사, 기대 판정, 라벨만 가진다."""
    _, expected = load_expected()
    for case in expected:
        assert set(case) == {"no", "company", "expected", "item_labels", "excluded_item_count"}
        assert set(case["expected"]) == {"gate", "core_family"}
        assert all(l == "공통" or l.startswith("JF_") for l in case["item_labels"])
