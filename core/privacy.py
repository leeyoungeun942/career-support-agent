"""외부 LLM으로 보낼 프로필 문장에서 연락처 형식을 찾는다 (정책 eligibility.md 16장, 이슈 #31).

형식(이메일, 전화번호, 주민등록번호, 링크)만 찾는다. 실명과 주소는 형식으로 구분할 수 없어 찾지 못한다.
발견해도 오류에는 위치와 종류만 적고 내용은 적지 않는다.
"""
import re

PATTERNS = (
    ("이메일", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("전화번호", re.compile(r"(?<!\d)(?:01[016789]|0\d{1,2})[-. ]?\d{3,4}[-. ]?\d{4}(?!\d)")),
    ("주민등록번호", re.compile(r"(?<!\d)\d{6}[-. ]?[1-4]\d{6}(?!\d)")),
    ("링크", re.compile(r"(?:https?://|www\.)\S+")),
)


def contact_findings(value, path: str = "") -> list[tuple[str, str]]:
    """(위치, 종류) 목록. 문자열, 목록, 딕셔너리를 따라 내려간다. 위치는 `experiences[1].actions` 꼴이다."""
    if isinstance(value, str):
        return [(path or "(최상위)", kind) for kind, pattern in PATTERNS if pattern.search(value)]
    if isinstance(value, dict):
        return [f for key, item in value.items() for f in contact_findings(item, f"{path}.{key}" if path else str(key))]
    if isinstance(value, (list, tuple)):
        return [f for index, item in enumerate(value) for f in contact_findings(item, f"{path}[{index}]")]
    return []


def reject_contact_info(value, what: str = "프로필") -> None:
    """연락처 형식이 있으면 PermissionError. 호출 전에 부른다. 메시지에는 위치와 종류만 있다."""
    found = contact_findings(value)
    if found:
        listed = ", ".join(f"{path}({kind})" for path, kind in found[:5]) + (f" 외 {len(found) - 5}곳" if len(found) > 5 else "")
        raise PermissionError(f"{what}에 연락처 형식이 있어 외부 LLM으로 보내지 않는다: {listed} (정책 eligibility.md 16장). 가상 프로필 문장에서 지운다")
