"""직무 게이트 mock AI와 평가 도구, 개발셋 30건 회귀 (이슈 #5, docs/policies/job-gate.md 6장).

개발셋 30건은 공고 원문이 필요하다. 원문은 커밋하지 않는 파일이라 `data/raw/eval_cases.json`이 없으면 해당 테스트를 건너뛴다.
확인셋(holdout)은 정책 6-4에 따라 다시 재지 않는다. 이 파일의 어떤 테스트도 확인셋을 돌리지 않는다.
"""
import json
import re
from collections import Counter

import pytest

import policy_docs as pd
from core import job_gate as gate
from core import job_gate_config as cfg
from core.job_gate_mock import make_label_mock
from core.job_gate_result import build_gate_result
from core.schemas import DISPLAY_LABELS
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
    assert len(events) == 1 and events[0]["case"] == "c-1" and events[0]["want"] == "JF_SALES" and len(events[0]["text"]) == 60


def test_label_mock_without_event_sink_still_works():
    assert make_label_mock(["JF_SALES"])(request(["JF_DEV"]))["label"] == gate.MIXED


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
    assert len(m["wrong"]) == 1 and len(m["rule_conflicts"]) == 1 and m["ai_calls"] == (0, 1)
    m = eval_gate.compute_metrics([(right, rec("match", "JF_SERVICE_PLANNING", "JF_SERVICE_PLANNING", source="ai"))])
    assert m["gate_ok"] == (1, 1) and m["wrong"] == [] and m["ai_calls"] == (1, 1)


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
    """개발셋 30/30: 판정, 핵심 직무군, 항목 라벨이 모두 정답과 같다 (6-4 2회차 이후 유지)."""
    metrics, rows, events = devset
    assert metrics["n"] == 30
    assert metrics["gate_ok"] == (30, 30) and metrics["core_ok"] == (30, 30) and metrics["item_ok"] == (155, 155)
    assert metrics["wrong"] == [] and events == []  # AI 라벨 목록 밖 정답(후보 탐색 누락)도 없다


@needs_devset
def test_devset_has_no_dangerous_errors(devset):
    metrics, _, _ = devset
    assert metrics["false_mismatch"] == (0, 7) and metrics["false_out_of_scope"] == (0, 7) and metrics["false_match"] == (0, 23)
    assert metrics["review_needed"] == (0, 30) and metrics["review_needed_expected"] == 0 and metrics["rule_conflicts"] == []


@needs_devset
def test_devset_ai_call_share_is_181_of_197(devset):
    """AI로 넘어간 하위 업무 수. 규칙 확정이 줄거나 늘면 이 숫자가 바뀐다(비용·속도 지표)."""
    assert devset[0]["ai_calls"] == (181, 197)


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
