"""프롬프트 파일 읽기. 맨 위 줄은 `version: N`이고 캐시 키에 들어간다 (정책 13-1)."""
import re
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str, directory: Path = PROMPT_DIR) -> tuple[str, str]:
    """(version, 본문)을 돌려준다. version 줄이 없으면 오류다. 버전이 없으면 프롬프트를 고쳐도 캐시가 갱신되지 않는다."""
    text = (directory / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"version:\s*(\S+)\s*\n", text)
    if not m:
        raise ValueError(f"{name}.md 맨 위에 'version: N' 줄이 없다")
    return m.group(1), text[m.end():].lstrip("\n")


def fill(body: str, **values: str) -> str:
    """`{{이름}}` 자리를 채운다. str.format을 쓰지 않는 이유: 본문에 JSON 중괄호가 있다. 값 안의 `{{...}}`는 다시 채우지 않는다."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"프롬프트 자리 {{{{{key}}}}}에 넣을 값이 없다")
        return values[key]

    return re.sub(r"\{\{(\w+)\}\}", sub, body)
