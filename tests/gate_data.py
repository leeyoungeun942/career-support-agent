"""샘플 공고 6건 데이터의 위치와 건너뛰기 표식 (직무 게이트 테스트가 함께 쓴다).

원문(`data/sample-jobs/gate_sample_cases.json`)은 커밋하지 않는 파일이라 없으면 원문이 필요한 테스트를 건너뛴다.
기대 판정(`tests/fixtures/job_gate_sample_expected.json`)에는 원문이 없고 커밋한다.
"""
import json
from pathlib import Path

import pytest

RAW_CASES = Path(__file__).resolve().parents[1] / "data" / "sample-jobs" / "gate_sample_cases.json"
EXPECTED = Path(__file__).parent / "fixtures" / "job_gate_sample_expected.json"

needs_raw = pytest.mark.skipif(
    not RAW_CASES.exists(),
    reason="샘플 공고 원문이 없어 건너뜀: data/sample-jobs/gate_sample_cases.json (팀 Drive에서 받아 넣는다. 커밋하지 않는 파일)",
)


def load_expected():
    """(희망 직무군, 공고별 기대 판정 목록). 원문 없이 읽힌다."""
    data = json.loads(EXPECTED.read_text(encoding="utf-8"))
    return data["desired_family"], data["cases"]
