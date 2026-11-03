"""직무 게이트 코드값이 정책 문서(docs/policies/job-gate.md)와 같은지 대조 (이슈 #5).

1-5의 판정 5종·사유 4종·경고 3종과 후속 처리(정렬 그룹, 표시 문구), 5-3 기준값을 문서 표에서 읽어 코드와 비교한다.
문서를 읽는 계약 테스트라 정책 문서 형식이 바뀌면 깨진다. 설정값 표(3-2, 3-4, 4-2)는 test_job_gate_config.py가 맡는다.
"""
import re
from typing import get_args

import pytest

import policy_docs as pd
from core import job_gate as gate
from core.schemas import DISPLAY_FLAGS, DISPLAY_LABELS, RANKING_GROUP, GateResultCode, ReasonCode, WarningCode

pytestmark = pytest.mark.policy_contract


def policy_1_5():
    """1-5 표를 (판정 행, 사유 행, 경고 행)으로. 각 행은 셀 목록이다."""
    results, reasons, warnings, mode = [], [], [], None
    for line in pd.section(pd.POLICY_MD.read_text(encoding="utf-8"), "### 1-5."):
        if not line.startswith("|"):
            continue
        cells = pd.table_cells(line)
        if set(cells[0]) <= {"-"}:
            continue
        if cells[0] in ("결과", "사유 코드", "경고 코드"):
            mode = cells[0]
            continue
        {"결과": results, "사유 코드": reasons, "경고 코드": warnings}[mode].append(cells)
    return results, reasons, warnings


def label_of(cell):
    """셀에서 **굵게** 또는 `백틱`으로 감싼 이름만 꺼낸다."""
    return cell.strip("*`")


def test_result_reason_and_warning_names_match_the_code_values():
    results, reasons, warnings = policy_1_5()
    assert {label_of(r[0]) for r in results} == {DISPLAY_LABELS[c] for c in get_args(GateResultCode)}
    assert {label_of(r[0]) for r in reasons} == {DISPLAY_LABELS[c] for c in get_args(ReasonCode)}
    assert {label_of(r[0]) for r in warnings} == {DISPLAY_LABELS[c] for c in get_args(WarningCode)}


def test_code_values_the_gate_can_emit_are_all_documented():
    """판정 코드가 만들 수 있는 값(소스에 적힌 리터럴)이 모두 1-5에 있는 이름이다."""
    source = (pd.REPO / "core" / "job_gate.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'finish\(\d, "(\w+)"(?:, "(\w+)")?\)', source))
    results = {c for pair in emitted for c in pair[:1]}
    reasons = {pair[1] for pair in emitted if pair[1]}
    assert results == {"out_of_scope", "review_needed", "match", "adjacent", "mismatch"} == set(get_args(GateResultCode))
    assert reasons == set(get_args(ReasonCode))
    warnings = set(re.findall(r'(?:append\(|\[)"(title_differs|unconfirmed_mapping|category_differs)"', source))
    assert warnings <= set(get_args(WarningCode)) and {"title_differs", "unconfirmed_mapping"} <= warnings  # 대분류 차이는 대분류 정보가 생기면 쓴다


def test_follow_up_column_matches_ranking_group_and_display_flag():
    """후속 처리 열: 정렬 그룹(`primary` 등)과 표시 문구("직무 범위 차이" 등)가 RANKING_GROUP, DISPLAY_FLAGS와 같다."""
    results, _, _ = policy_1_5()
    to_code = {DISPLAY_LABELS[c]: c for c in get_args(GateResultCode)}
    assert len(results) == 5
    for name, _, follow_up in results:
        code = to_code[label_of(name)]
        group = re.search(r"`(primary|job_mismatch|out_of_scope)`", follow_up)
        expected_group = group.group(1) if group else "primary" if "상위 그룹" in follow_up else None
        assert expected_group == RANKING_GROUP[code], (name, follow_up)
        flag = re.search(r'"([^"]+)" 표시', follow_up)
        assert (flag.group(1) if flag else None) == (DISPLAY_LABELS[DISPLAY_FLAGS[code]] if DISPLAY_FLAGS[code] else None), (name, follow_up)


def policy_5_3_thresholds():
    rows = {}
    for line in pd.section(pd.POLICY_MD.read_text(encoding="utf-8"), "### 5-3."):
        if line.startswith("|"):
            cells = pd.table_cells(line)
            if len(cells) >= 2 and cells[0] not in ("항목",) and not set(cells[0]) <= {"-"}:
                rows[cells[0]] = cells[1]
    return rows


def test_thresholds_match_policy_5_3_table():
    rows = policy_5_3_thresholds()
    min_items = int(re.search(r"\*\*(\d+)개\*\* 미만", rows["정보 부족 기준"]).group(1))
    num, den = re.search(r"\*\*(\d+)/(\d+) 이상\*\*", rows["업무 판정 명확성"]).groups()
    assert gate.GATE_THRESHOLDS["min_items"] == min_items
    assert gate.GATE_THRESHOLDS["clear_ratio"] == pytest.approx(int(num) / int(den))
    assert set(gate.GATE_THRESHOLDS) == {"min_items", "clear_ratio"}  # 표에 없는 기준값을 코드에 몰래 두지 않는다
