"""직무 게이트 mock AI와 평가 도구, 개발셋 30건 회귀 (이슈 #5, docs/policies/job-gate.md 6장).

개발셋 30건은 공고 원문이 필요하다. 원문은 커밋하지 않는 파일이라 `data/raw/eval_cases.json`이 없으면 해당 테스트를 건너뛴다.
확인셋(holdout)은 정책 6-4에 따라 다시 재지 않는다. 이 파일의 어떤 테스트도 확인셋을 돌리지 않는다.
"""
import json
import re
from collections import Counter
from typing import get_args

import pytest

import policy_docs as pd
from core import job_gate as gate
from core import job_gate_config as cfg
from core.job_gate_mock import make_label_mock
from core.job_gate_result import build_gate_result
from core.schemas import DISPLAY_LABELS, GateResultCode, ReasonCode, WarningCode
from tools import eval_gate

needs_devset = pytest.mark.skipif(
    not eval_gate.CASES_JSON.exists(),
    reason="공고 원문·정답 파일이 없어 건너뜀: data/raw/eval_cases.json (팀 Drive에서 받아 data/raw/에 넣는다. 커밋하지 않는 파일)",
)


# ---------------------------------------------------------------------
# mock AI (원문 없이 돈다)
# ---------------------------------------------------------------------
def request(allowed, item_index=0, text="업무 문장"):
    return {"level": "subtask", "item_index": item_index, "sub_index": 0, "text": text, "item_text": text,
            "other_items": [], "candidates": [], "allowed": allowed, "families": {}}


def test_label_mock_returns_the_answer_label_when_allowed():
    mock = make_label_mock(["JF_DEV", gate.COMMON])
    assert mock(request(["JF_DEV", gate.COMMON], item_index=0))["label"] == "JF_DEV"
    assert mock(request(["JF_DEV", gate.COMMON], item_index=1))["label"] == gate.COMMON


def test_label_mock_falls_back_to_mixed_and_records_when_the_answer_is_not_allowed():
    events = []
    mock = make_label_mock(["JF_SALES"], events, case_id="c-1")
    answer = mock(request(["JF_DEV", gate.COMMON, gate.OUT_OF_SCOPE, gate.MIXED], text="가" * 100))
    assert answer["label"] == gate.MIXED  # 정답이 라벨 목록 밖이면 정답을 낼 수 없다(후보 탐색 누락)
    allowed = ["JF_DEV", gate.COMMON, gate.OUT_OF_SCOPE, gate.MIXED]
    assert events == [{"case": "c-1", "item": 1, "want": "JF_SALES", "allowed": allowed, "text": "가" * 60}]  # item은 1부터 센다


def test_label_mock_without_event_sink_still_works():
    assert make_label_mock(["JF_SALES"])(request(["JF_DEV"]))["label"] == gate.MIXED


# ---------------------------------------------------------------------
# 평가 도구: 경로, 대응표 (원문 없이 돈다)
# ---------------------------------------------------------------------
def test_answer_file_path_is_the_documented_one():
    """경로가 틀리면 개발셋 테스트가 '파일 없음'으로 건너뛰어져 통과처럼 보인다. 경로를 따로 고정한다."""
    assert eval_gate.REPO == pd.REPO
    assert eval_gate.CASES_JSON == pd.REPO / "data" / "raw" / "eval_cases.json"


def test_raw_korean_tables_agree_with_display_labels_and_cover_every_code():
    """정답 파일의 한글 표기가 DISPLAY_LABELS의 한글과 같고, 모든 코드가 빠짐없이 대응한다."""
    for raw_table, codes in ((eval_gate.RAW_GATE, get_args(GateResultCode)), (eval_gate.RAW_REASON, get_args(ReasonCode)),
                             (eval_gate.RAW_WARNING, get_args(WarningCode))):
        assert set(raw_table.values()) == set(codes)
        assert all(DISPLAY_LABELS[code] == korean for korean, code in raw_table.items())
    # 항목 라벨은 정답 파일이 `범위외`(띄어쓰기 없음)로 적는다. 표시 문구 `범위 외`와 글자가 다른 것은 알려진 차이다
    assert eval_gate.RAW_LABEL == {"공통": "common", "범위외": "out_of_scope", "혼합": "mixed"}


# ---------------------------------------------------------------------
# 평가 도구: 읽기와 지표 (합성 데이터, 원문 없이 돈다)
# ---------------------------------------------------------------------
def eval_file(tmp_path, cases):
    path = tmp_path / "eval_cases.json"
    path.write_text(json.dumps({"desired_family": "JF_SERVICE_PLANNING", "cases": cases}, ensure_ascii=False), encoding="utf-8")
    return path


def case(cid, set_name="dev", status="approved", hold=False, gate_ko="불일치", labels=("JF_DEV", "JF_DEV"), core="JF_DEV", title="담당자"):
    return {"id": cid, "set": set_name, "job_title": title, "position": "",
            "items": [{"text": f"업무 {i}를 처리합니다"} for i in range(len(labels))],
            "answer": {"review_status": status, "hold": hold, "gate": gate_ko, "core_family": core, "reason_code": None, "warnings": [],
                       "item_labels": [{"label": l, "item": f"업무 {i}"} for i, l in enumerate(labels)]}}


def test_load_converts_korean_answer_labels_to_codes(tmp_path):
    path = eval_file(tmp_path, [case("a", gate_ko="범위 외", labels=("범위외", "공통", "혼합", "JF_DEV"), core=None),
                                {**case("b", gate_ko="검토 필요", labels=("공통",), core=None), "answer": {
                                    **case("b", gate_ko="검토 필요", labels=("공통",), core=None)["answer"], "reason_code": "정보 부족", "warnings": ["직무명 차이"]}}])
    desired, cases, skipped = eval_gate.load("dev", path)
    assert desired == "JF_SERVICE_PLANNING" and skipped == []
    a, b = cases
    assert a["answer"]["gate"] == "out_of_scope"
    assert [x["label"] for x in a["answer"]["item_labels"]] == [gate.OUT_OF_SCOPE, gate.COMMON, gate.MIXED, "JF_DEV"]
    assert (b["answer"]["gate"], b["answer"]["reason_code"], b["answer"]["warnings"]) == ("review_needed", "insufficient_info", ["title_differs"])


def test_load_does_not_change_the_data_file_and_does_not_mutate_between_calls(tmp_path):
    path = eval_file(tmp_path, [case("a")])
    before = path.read_text(encoding="utf-8")
    eval_gate.load("dev", path)
    eval_gate.load("dev", path)
    assert path.read_text(encoding="utf-8") == before  # 데이터 파일은 그대로 둔다


def test_load_skips_an_answer_without_review_status_instead_of_crashing(tmp_path):
    bad = case("no-status")
    del bad["answer"]["review_status"]
    _, cases, skipped = eval_gate.load("dev", eval_file(tmp_path, [case("ok"), bad]))
    assert [c["id"] for c in cases] == ["ok"] and [c["id"] for c in skipped] == ["no-status"]


def test_load_reports_which_case_has_the_wrong_number_of_item_labels(tmp_path):
    bad = case("bad-labels", labels=("JF_DEV", "JF_DEV"))
    bad["answer"]["item_labels"] = bad["answer"]["item_labels"][:1]
    with pytest.raises(ValueError, match="정답 파일 오류: 공고 bad-labels의 item_labels 1개, 업무 항목 2개"):
        eval_gate.load("dev", eval_file(tmp_path, [case("ok"), bad]))
    # 회사 소개 문장(exclude)은 항목 수에 세지 않는다
    ok = case("with-excluded", labels=("JF_DEV",))
    ok["items"].append({"text": "회사 소개 문장입니다", "exclude": True})
    assert [c["id"] for c in eval_gate.load("dev", eval_file(tmp_path, [ok]))[1]] == ["with-excluded"]


def test_load_selects_the_set_and_skips_unapproved_and_held_answers(tmp_path):
    path = eval_file(tmp_path, [case("dev-ok"), case("dev-draft", status="draft"), case("dev-hold", hold=True),
                                case("hold-ok", set_name="holdout"), {**case("dev-none"), "answer": None}])
    _, dev, skipped = eval_gate.load("dev", path)
    assert [c["id"] for c in dev] == ["dev-ok"]
    assert sorted(c["id"] for c in skipped) == ["dev-draft", "dev-hold", "dev-none"]
    assert [c["id"] for c in eval_gate.load("holdout", path)[1]] == ["hold-ok"]
    assert [c["id"] for c in eval_gate.load("all", path)[1]] == ["dev-ok", "hold-ok"]


def test_main_without_arguments_evaluates_only_the_dev_set(tmp_path, monkeypatch, capsys):
    """확인셋은 기본으로 돌리지 않는다 (6-4). 합성 데이터에 확인셋 공고를 넣어도 인자 없이는 개발셋만 평가한다."""
    monkeypatch.setattr(eval_gate, "CASES_JSON", eval_file(tmp_path, [case("dev-1"), case("hold-1", set_name="holdout")]))
    eval_gate.main([])
    out = capsys.readouterr().out
    assert "=== dev (mock)" in out and "확인셋입니다" not in out
    assert "기준 판정과의 일치율      1/1 (100%)" in out  # 개발셋 1건만 채점했다


def test_main_warns_before_running_the_holdout_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(eval_gate, "CASES_JSON", eval_file(tmp_path, [case("dev-1"), case("hold-1", set_name="holdout")]))
    eval_gate.main(["--set", "holdout"])
    out = capsys.readouterr().out
    assert "확인셋입니다. 결과를 보고 규칙·기준값을 고치면 안 됩니다" in out and "=== holdout (mock)" in out


def test_main_reports_a_missing_answer_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(eval_gate, "CASES_JSON", tmp_path / "없는파일.json")
    with pytest.raises(SystemExit) as exit_info:
        eval_gate.main([])
    assert exit_info.value.code == 2 and "정답 파일이 없다" in capsys.readouterr().err


def test_ai_option_exits_with_a_message_and_never_calls_anything(capsys):
    with pytest.raises(SystemExit) as exit_info:
        eval_gate.main(["--ai"])
    assert exit_info.value.code == 2
    assert "아직 연결하지 않았다" in capsys.readouterr().err
    with pytest.raises(NotImplementedError):
        eval_gate.evaluate("dev", use_ai=True)


def test_evaluate_prints_the_6_3_metrics_for_a_tiny_set(tmp_path, capsys):
    # 개발 업무 2개 공고(정답: 불일치/개발) 1건 + 서비스기획 업무 2개 공고(정답: 일치/서비스기획) 1건
    path = eval_file(tmp_path, [case("x1", labels=("JF_DEV", "JF_DEV")),
                                case("x2", gate_ko="일치", labels=("JF_SERVICE_PLANNING", "JF_SERVICE_PLANNING"), core="JF_SERVICE_PLANNING")])
    metrics, rows, events = eval_gate.evaluate("dev", path=path)
    assert metrics["gate_ok"] == (2, 2) and metrics["core_ok"] == (2, 2) and metrics["item_ok"] == (4, 4)
    assert metrics["false_mismatch"] == (0, 1) and metrics["false_match"] == (0, 1) and metrics["wrong"] == [] and events == []
    assert metrics["confusion"] == Counter({("mismatch", "mismatch"): 1, ("match", "match"): 1})
    out = capsys.readouterr().out
    assert "기준 판정과의 일치율      2/2 (100%)" in out and "거짓 불일치" in out and "AI 호출 비중" in out


def test_compute_metrics_counts_wrong_results():
    right = {"id": "r", "answer": {"gate": "match", "core_family": "JF_SERVICE_PLANNING", "item_labels": [{"label": "JF_SERVICE_PLANNING"}]}}
    rec = lambda result, core, label, source="rule": {"result": result, "core_family": core, "items": [  # noqa: E731
        {"index": 0, "label": label, "subtasks": [{"source": source, "label": label, "text": "t"}]}]}
    # 일치 정답을 불일치로 낸 공고(거짓 불일치), 그 항목 라벨도 틀림, 규칙이 확정한 하위 업무가 항목 정답과 다름
    m = eval_gate.compute_metrics([(right, rec("mismatch", "JF_DEV", "JF_DEV"))])
    assert m["gate_ok"] == (0, 1) and m["false_mismatch"] == (1, 1) and m["core_ok"] == (0, 1) and m["item_ok"] == (0, 1)
    assert len(m["wrong"]) == 1 and m["ai_calls"] == (0, 1)
    assert m["rule_conflicts"] == [("r", 1, "JF_DEV", "JF_SERVICE_PLANNING", "t")]  # (공고 id, 항목 번호(1부터), 규칙 라벨, 정답, 문구)
    m = eval_gate.compute_metrics([(right, rec("match", "JF_SERVICE_PLANNING", "JF_SERVICE_PLANNING", source="ai"))])
    assert m["gate_ok"] == (1, 1) and m["wrong"] == [] and m["ai_calls"] == (1, 1)


@pytest.mark.parametrize("result,core,label,wrong", [
    ("match", "JF_SERVICE_PLANNING", "JF_SERVICE_PLANNING", 0),     # 모두 정답
    ("mismatch", "JF_SERVICE_PLANNING", "JF_SERVICE_PLANNING", 1),  # 판정만 다름
    ("match", "JF_DEV", "JF_SERVICE_PLANNING", 1),                  # 핵심 직무군만 다름
    ("match", "JF_SERVICE_PLANNING", "JF_DEV", 1),                  # 항목 라벨만 다름
])
def test_wrong_cases_are_detected_for_each_kind_of_difference(result, core, label, wrong):
    answer = {"id": "r", "answer": {"gate": "match", "core_family": "JF_SERVICE_PLANNING", "item_labels": [{"label": "JF_SERVICE_PLANNING"}]}}
    record = {"result": result, "core_family": core, "items": [
        {"index": 0, "label": label, "subtasks": [{"source": "ai", "label": label, "text": "t"}]}]}
    assert len(eval_gate.compute_metrics([(answer, record)])["wrong"]) == wrong


SP, DEV = "JF_SERVICE_PLANNING", "JF_DEV"


def report_case(cid, labels, gate_ko, core, texts=None, title="담당자", status="approved", note=None):
    c = case(cid, labels=tuple(labels), gate_ko=gate_ko, core=core, title=title)
    if texts:
        c["items"] = [{"text": t} for t in texts]
    c["answer"]["review_status"] = status
    if note:
        c["answer"]["note"] = note
    return c


@pytest.fixture
def wrong_report(tmp_path, capsys):
    """틀린 공고, 제외된 공고, AI 라벨 목록 밖 정답이 모두 있는 합성 세트를 돌린 보고서(자세히 보기)."""
    path = eval_file(tmp_path, [
        report_case("ok-1", [DEV, DEV], "불일치", DEV),
        report_case("skip-draft", [DEV, DEV], "불일치", DEV, status="draft"),
        report_case("wrong-gate", [DEV, DEV], "일치", SP),
        report_case("wrong-item", [DEV, DEV], "불일치", DEV, texts=["서비스 기획서 작성", "서비스 기획서 검토"], note="정답 설명 문장"),
        report_case("wrong-short", [DEV], "일치", SP),
        report_case("wrong-warn", [SP, SP, SP, SP], "불일치", SP, title="백엔드 개발자 모집"),
        report_case("event-1", ["JF_NOPE", "JF_NOPE"], "불일치", DEV),
    ])
    metrics, rows, events = eval_gate.evaluate("dev", verbose=True, path=path)
    return metrics, events, capsys.readouterr().out


def test_report_lists_each_wrong_case_with_its_reason(wrong_report):
    metrics, events, out = wrong_report
    assert [c["id"] for c, _ in metrics["wrong"]] == ["wrong-gate", "wrong-item", "wrong-short", "wrong-warn", "event-1"]
    assert "채점 제외 1건 (미확정·보류)" in out
    assert f"--- 정답과 다른 공고 {len(metrics['wrong'])}건 ---" in out
    assert "[wrong-gate] 담당자 | 정답 일치/서비스기획 → 결과 불일치/개발 (끝난 단계 6)" in out
    assert "[wrong-short] 담당자 | 정답 일치/서비스기획 → 결과 검토 필요/- (끝난 단계 2, 정보 부족)" in out  # 핵심 직무군이 없으면 -
    assert "(항목 라벨링 전에 끝남: 2단계)" in out
    assert "[wrong-warn] 백엔드 개발자 모집 | 정답 불일치/서비스기획 → 결과 일치/서비스기획 (끝난 단계 6, 경고 ['직무명 차이'])" in out


def test_report_marks_wrong_and_right_items_and_shows_rule_evidence(wrong_report):
    _, _, out = wrong_report
    assert "X 항목 1 서비스기획 (정답 개발): 서비스 기획서 작성" in out and "X 항목 2 서비스기획 (정답 개발): 서비스 기획서 검토" in out
    assert "O 항목 1 개발 (정답 개발): 업무 0를 처리합니다" in out  # 자세히 보기(-v)에서는 맞은 항목도 보인다
    assert "[rule] 서비스기획: 서비스 기획서 작성 단서={'SERVICE_PLANNING':" in out  # 틀린 항목은 하위 업무와 단서를 보여 준다
    assert "정답 근거: 정답 설명 문장" in out
    assert "? 규칙 확정 충돌: [wrong-item] 항목 1 서비스기획 (정답 개발): 서비스 기획서 작성" in out


def test_report_lists_answers_outside_the_ai_label_list(wrong_report):
    _, events, out = wrong_report
    assert [(e["case"], e["item"], e["want"]) for e in events] == [("event-1", 1, "JF_NOPE"), ("event-1", 2, "JF_NOPE")]
    assert "정답이 AI 라벨 목록 밖     2건 (후보 탐색 누락)" in out
    assert "! AI 라벨 목록 밖: [event-1] 항목 1 정답 JF_NOPE, 허용" in out and "! AI 라벨 목록 밖: [event-1] 항목 2 정답 JF_NOPE" in out


def test_report_confusion_lines_put_correct_ones_first_then_sort_by_korean_display(wrong_report):
    _, _, out = wrong_report
    lines = out.split("판정 분포 (정답 → 결과):\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert [l.split() for l in lines] == [
        ["불일치", "→", "불일치", "1건"],                               # 정답과 같은 줄이 먼저
        ["불일치", "→", "검토", "필요", "1건", "<--", "다름"],            # 그 다음은 정답, 결과의 한글 순서
        ["불일치", "→", "일치", "2건", "<--", "다름"],
        ["일치", "→", "검토", "필요", "1건", "<--", "다름"],
        ["일치", "→", "불일치", "1건", "<--", "다름"],
    ]


def test_report_without_verbose_only_shows_the_wrong_items(tmp_path, capsys):
    path = eval_file(tmp_path, [report_case("wrong-item", [DEV, DEV], "불일치", DEV, texts=["서비스 기획서 작성", "서비스 기획서 검토"], note="정답 설명 문장")])
    eval_gate.evaluate("dev", verbose=False, path=path)
    out = capsys.readouterr().out
    assert "X 항목 1 서비스기획" in out and "정답 근거" not in out
    assert "채점 제외" not in out  # 제외된 공고가 없으면 줄을 내지 않는다


def test_report_for_a_clean_set_has_no_wrong_section(tmp_path, capsys):
    eval_gate.evaluate("dev", path=eval_file(tmp_path, [report_case("ok-1", [DEV, DEV], "불일치", DEV)]))
    out = capsys.readouterr().out
    assert "정답과 다른 공고" not in out and "<-- 다름" not in out and "? 규칙" not in out and "! AI" not in out


# ---------------------------------------------------------------------
# 개발셋 30건 회귀 (원문 필요)
# ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def devset():
    import io
    from contextlib import redirect_stdout
    with redirect_stdout(io.StringIO()):  # 도구가 출력하는 보고서는 테스트 로그에 쓰지 않는다
        return eval_gate.evaluate("dev")


@needs_devset
def test_devset_every_case_matches_its_answer(devset):
    """개발셋 30/30: 판정, 핵심 직무군, 항목 라벨이 모두 정답과 같다 (6-4 2회차 이후 유지, 5회차에서 항목 170개)."""
    metrics, rows, events = devset
    assert metrics["n"] == 30
    assert metrics["gate_ok"] == (30, 30) and metrics["core_ok"] == (30, 30) and metrics["item_ok"] == (170, 170)
    assert metrics["wrong"] == [] and events == []  # AI 라벨 목록 밖 정답(후보 탐색 누락)도 없다


@needs_devset
def test_devset_has_no_dangerous_errors(devset):
    metrics, _, _ = devset
    assert metrics["false_mismatch"] == (0, 7) and metrics["false_out_of_scope"] == (0, 7) and metrics["false_match"] == (0, 23)
    assert metrics["review_needed"] == (0, 30) and metrics["review_needed_expected"] == 0 and metrics["rule_conflicts"] == []


@needs_devset
def test_devset_ai_call_share_is_197_of_213(devset):
    """AI로 넘어간 하위 업무 수. 규칙 확정이 줄거나 늘면 이 숫자가 바뀐다(비용·속도 지표). 정책 6-4 5회차와 같다."""
    assert devset[0]["ai_calls"] == (197, 213)


@needs_devset
def test_devset_results_convert_to_valid_v2_results(devset):
    """실제 공고 30건으로 v2 변환을 확인한다: 스키마 통과, 인용이 원문의 일부, 판정이 정답과 같다."""
    _, rows, _ = devset
    for case_, record in rows:
        r = build_gate_result(f"wanted-{case_['id']}", record)
        assert r.result == case_["answer"]["gate"] and r.core_family == case_["answer"]["core_family"], case_["id"]
        assert r.ranking_group == ("primary" if r.result in ("match", "adjacent", "review_needed") else
                                   "job_mismatch" if r.result == "mismatch" else "out_of_scope")
        for e in r.detail.evidence.supporting + r.detail.evidence.conflicting:
            item = r.detail.items[e.item_index]
            assert e.quote in f"{item.heading} {item.text}"  # 스키마도 검사한다
        assert r.warnings == case_["answer"]["warnings"], case_["id"]
        if r.reason_code:
            assert r.reason_code == case_["answer"]["reason_code"]


@pytest.mark.policy_contract
@needs_devset
def test_devset_metrics_equal_the_latest_dev_row_in_policy_6_4(devset):
    """다시 계산한 개발셋 지표가 정책 6-4 표에 적힌 같은 설정값의 개발셋 행과 같다. 문서의 기록과 코드가 어긋나면 잡힌다."""
    rows = [r for r in pd.doc_validation_rows() if r["셋"].startswith("개발셋") and r["설정값"] == cfg.CONFIG_VERSION]
    assert rows, f"6-4 표에 설정값 {cfg.CONFIG_VERSION}의 개발셋 행이 없다 (설정값을 올렸다면 개발셋을 다시 돌려 6-4에 기록한다)"
    row = rows[-1]
    metrics = devset[0]
    cell = lambda name: tuple(int(x) for x in re.match(r"(\d+)/(\d+)", row[name]).groups())  # noqa: E731
    assert cell("판정 일치") == metrics["gate_ok"]
    assert cell("거짓 불일치") == metrics["false_mismatch"]
    assert cell("거짓 범위 외") == metrics["false_out_of_scope"]
    assert cell("거짓 일치") == metrics["false_match"]
    assert cell("검토 필요") == metrics["review_needed"]
    assert cell("핵심 직무군 일치") == metrics["core_ok"]
    assert cell("항목 라벨 일치") == metrics["item_ok"]
    assert cell("AI 호출 비중") == metrics["ai_calls"]
    assert row["기준값"] == "정보 부족 2개 미만, 명확성 2/3"
    assert (gate.GATE_THRESHOLDS["min_items"], round(gate.GATE_THRESHOLDS["clear_ratio"], 4)) == (2, round(2 / 3, 4))


@needs_devset
def test_loaded_devset_contains_no_holdout_cases():
    _, cases, _ = eval_gate.load("dev")
    assert len(cases) == 30 and {c["set"] for c in cases} == {"dev"}
