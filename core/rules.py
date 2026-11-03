"""자격요건 평가에서 코드가 하는 판정 (docs/policies/eligibility.md, 이슈 #4).

- LLM과 네트워크를 쓰지 않는다. 점수 계산, 판정 형식 변환, 순위 정렬은 이 파일이 한다 (AGENTS.md).
- 이 파일과 정책 문서가 다르면 이 파일을 문서에 맞춘다. 문서가 미확정이거나 모순이면 먼저 문서를 확정한 뒤 맞춘다.
"""
from collections.abc import Iterable

from core.schemas import Experience, month_index

# =====================================================================
# 경력 기간 합산 (eligibility.md 4장, 11장)
# =====================================================================

# 연수에 넣는 경험 유형. project, activity는 연수에 넣지 않는다 (4장)
COUNTED_EXPERIENCE_TYPES = frozenset({"intern", "full_time", "contract"})


def career_experiences(experiences: Iterable[Experience]) -> list[Experience]:
    """연수에 넣는 경험만 남긴다."""
    return [e for e in experiences if e.type in COUNTED_EXPERIENCE_TYPES]


def experience_months(exp: Experience, as_of_month: str) -> set[int]:
    """경험 하나가 차지하는 달(월 번호) 집합. 시작 월과 끝 월을 모두 세고, 끝은 min(end, as_of_month)다.

    end가 없으면(진행 중) as_of_month까지다. 시작이 기준 월보다 늦으면 빈 집합이다.
    """
    as_of = month_index(as_of_month)
    end = month_index(exp.period.end) if exp.period.end is not None else as_of
    return set(range(month_index(exp.period.start), min(end, as_of) + 1))


def month_set(experiences: Iterable[Experience], as_of_month: str) -> set[int]:
    """여러 경험의 달 합집합. 겹친 달은 한 번만 들어간다."""
    months: set[int] = set()
    for exp in experiences:
        months |= experience_months(exp, as_of_month)
    return months


def count_months(experiences: Iterable[Experience], as_of_month: str) -> int:
    """여러 경험의 합산 개월 수. 겹친 달은 한 번만 센다 (단순 합산이 아니다)."""
    return len(month_set(experiences, as_of_month))
