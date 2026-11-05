"""프롬프트 파일 읽기. 맨 위 줄은 `version: N`이고 캐시 키에 들어간다 (정책 13-1)."""
import hashlib
import re
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str, directory: Path = PROMPT_DIR) -> tuple[str, str]:
    """(version, 본문)을 돌려준다. version 줄이 없으면 오류다. 버전이 없으면 프롬프트를 고쳐도 캐시가 갱신되지 않는다."""
    return _read(directory / f"{name}.md")


def _read(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"version:\s*(\S+)\s*\n", text)
    if not m:
        raise ValueError(f"{path.name} 맨 위에 'version: N' 줄이 없다")
    return m.group(1), text[m.end():].lstrip("\n")


def load_prompt_file(path: str | Path, root: Path = PROMPT_DIR) -> tuple[str, str]:
    """실험용 프롬프트 파일을 읽는다. root(prompts/) 아래의 `.md` 파일만 허용하고 밖의 경로는 ValueError다.

    공고 원문이 든 파일이나 키가 든 파일이 프롬프트로 나가지 않게 한다. 오류 메시지에는 파일 내용을 넣지 않는다.
    """
    target = Path(path).resolve()
    if target.suffix != ".md" or not target.is_relative_to(root.resolve()):
        raise ValueError(f"프롬프트 파일은 {root.name}/ 아래의 .md 파일이어야 한다")
    if not target.is_file():
        raise ValueError(f"프롬프트 파일이 없다: {target.name}")
    return _read(target)


def prompt_hash(body: str) -> str:
    """본문 해시의 앞 8자리. 같은 버전 문자열로 내용이 바뀐 것을 실행 기록에서 가려내는 용도다 (정책 6-5)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def fill(body: str, **values: str) -> str:
    """`{{이름}}` 자리를 채운다. str.format을 쓰지 않는 이유: 본문에 JSON 중괄호가 있다. 값 안의 `{{...}}`는 다시 채우지 않는다."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"프롬프트 자리 {{{{{key}}}}}에 넣을 값이 없다")
        return values[key]

    return re.sub(r"\{\{(\w+)\}\}", sub, body)
