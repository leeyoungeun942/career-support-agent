"""LLM 호출 모듈 자리 (이슈 #6). 아직 구현하지 않았다.

- 호출, 재시도, 응답을 스키마로 검증하는 일은 이 모듈에서만 한다 (project-guide 3-2 "LLM 호출 모듈을 분리").
- core/rules.py는 이 모듈을 가져오지 않는다. 점수 계산, 판정 형식 변환, 순위 정렬은 코드가 한다 (AGENTS.md).
- 기본은 mock이다. 실제 호출은 --ai 옵션과 외부 API 사용 승인 뒤에만 한다 (project-guide 3-2).
- 응답이 스키마를 어기면 오류로 처리하고 extraction_issues에 extraction_failed로 남긴다 (정책 1-4).
"""
from typing import Protocol


class LLMClient(Protocol):
    """요건 추출과 근거 찾기가 쓰는 호출 창구. 메서드 형태는 이슈 #6에서 확정한다."""

    def complete_json(self, prompt: str) -> dict:
        ...
