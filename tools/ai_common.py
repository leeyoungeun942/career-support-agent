"""--ai 실행의 공통 준비 (tools.eval_eligibility, tools.run_demo, tools.eval_gate가 함께 쓴다; 이슈 #12 Step 3).

안내 문구 출력, 약관 동의, 설정 읽기, 하루 호출 예산 점검, 캐시를 거치는 클라이언트 만들기를 한 곳에서 한다.
하나라도 안 되면 아무것도 보내지 않고 AiSetupError를 낸다. 메시지에는 키나 입력 내용이 없다.
환경 읽기(env_loader)와 클라이언트 클래스(client_cls)는 호출하는 쪽이 넘긴다. 테스트가 호출하는 쪽의 이름을 바꿔 끼울 수 있게 하려는 것이다.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.llm_client import (
    CachedClient,
    GeminiClient,
    GeminiSettings,
    LLMConfigError,
    LLMQuotaExceeded,
    UsageLog,
)


class AiSetupError(Exception):
    """실제 AI 실행을 시작할 수 없다. code는 프로세스 종료 코드다."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


@dataclass
class AiSession:
    """준비를 마친 실제 AI 실행: 설정, 사용량 기록, 캐시를 거치는 클라이언트."""

    settings: GeminiSettings
    usage: UsageLog
    gemini: GeminiClient
    client: CachedClient
    calls_before: int  # 시작 전에 오늘(태평양 시간) 이 모델로 이미 쓴 호출 수
    postings: dict[str, str] = field(default_factory=dict)  # eval_eligibility/run_demo가 채운다


def prepare_session(
    args, *, notice: str, needed_calls: int, results_dir: Path, cache_dir_name: str,
    env_loader: Callable[[], dict], client_cls: Callable, before_client: Callable[[], None] | None = None,
) -> AiSession:
    """안내 -> 동의 -> 설정 -> (before_client) -> 하루 예산 점검 -> 캐시를 거치는 클라이언트.

    needed_calls는 재시도까지 계산한 **최대 시도 수**를 넘긴다(남은 하루 한도와 비교한다). args에서 읽는 값:
    accept_free_tier_terms, model/rpm/rpd(덮어쓰기), fresh_cache, cache_profile(없으면 False).
    """
    results_dir = Path(results_dir)
    print(notice)
    if not args.accept_free_tier_terms:
        raise AiSetupError("위 조건에 동의하면 --accept-free-tier-terms를 붙여 다시 실행하라. 아직 아무것도 보내지 않았다.")
    env = env_loader()
    for flag, key in (("model", "GEMINI_MODEL"), ("rpm", "GEMINI_RPM"), ("rpd", "GEMINI_RPD")):
        if getattr(args, flag) is not None:
            env[key] = str(getattr(args, flag))
    try:
        settings = GeminiSettings.from_env(env)
    except LLMConfigError as e:
        raise AiSetupError(f"실행할 수 없다: {e}") from None
    if before_client:
        before_client()
    usage = UsageLog(results_dir / "llm_usage.jsonl")
    gemini = client_cls(settings, usage)
    try:
        gemini.ensure_budget(needed_calls)
    except LLMQuotaExceeded as e:
        raise AiSetupError(f"시작하지 않았다: {e}") from None
    cache_dir = results_dir / "llm_cache" / (f"run-{usage.run_id}" if args.fresh_cache else cache_dir_name)
    client = CachedClient(gemini, usage=usage, extract_dir=cache_dir, profile_dir=results_dir / "llm_cache" / "profile",
                          cache_profile=getattr(args, "cache_profile", False))
    return AiSession(settings, usage, gemini, client, usage.calls_today(settings.model))


def extra_cached_client(session: AiSession, args, *, results_dir: Path, cache_dir_name: str) -> CachedClient:
    """같은 GeminiClient·사용량 기록을 감싼 캐시 클라이언트를 하나 더 만든다. 다른 캐시 디렉터리를 쓰려는 단계(게이트 `gate`)용이다.

    한도와 사용량은 세션과 같이 쓴다(같은 키, 같은 모델). --fresh-cache이면 이번 실행만의 디렉터리(run-<실행 ID>-<이름>)를 쓴다.
    프로필이 들어가는 요청은 이 클라이언트로 보내지 않으므로 프로필 캐시는 켜지 않는다.
    """
    results_dir = Path(results_dir)
    name = f"run-{session.usage.run_id}-{cache_dir_name}" if args.fresh_cache else cache_dir_name
    return CachedClient(session.gemini, usage=session.usage, extract_dir=results_dir / "llm_cache" / name,
                        profile_dir=results_dir / "llm_cache" / "profile", cache_profile=False)
