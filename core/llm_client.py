"""LLM 호출 모듈 (이슈 #6, 정책 eligibility.md 13-1, 16장).

- 호출, 재시도, 캐시, 사용량 기록, 응답을 스키마로 검증하는 일은 이 모듈에서만 한다 (project-guide 3-2 "LLM 호출 모듈을 분리").
- core/rules.py는 이 모듈을 가져오지 않는다. 점수 계산, 판정 형식 변환, 순위 정렬은 코드가 한다 (AGENTS.md).
- 기본은 mock이다. 실제 호출(GeminiClient)은 --ai 옵션과 외부 API 사용 승인 뒤에만 한다 (project-guide 3-2).
- 응답이 스키마를 어기면 다시 묻지 않고 LLMStageError로 처리한다. 원시 오류와 모델 출력은 예외 메시지에 넣지 않는다 (정책 13-1).
- `google-genai`는 GeminiClient가 호출할 때만 가져온다. SDK가 없어도 이 모듈은 import된다.
- API 키는 예외·로그·캐시·사용량 기록에 쓰지 않는다 (정책 16장).
"""
import hashlib
import json
import os
import re
import time
import uuid
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

PACIFIC = ZoneInfo("America/Los_Angeles")  # 무료 티어 일일 한도(RPD)는 태평양 시간 자정에 초기화된다
USAGE_LOG_PATH = Path("data/results/llm_usage.jsonl")
EXTRACT_CACHE_DIR = Path("data/results/llm_cache/extract")
PROFILE_CACHE_DIR = Path("data/results/llm_cache/profile")

DEFAULT_RPM = 5   # 알려진 가장 작은 무료 한도. 실행 때 AI Studio 화면 값으로 GEMINI_RPM을 맞춘다 (정책 18장)
DEFAULT_RPD = 20
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0
MAX_WAIT_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 120  # 응답이 멎은 호출을 끊는다. 시간 초과는 네트워크 오류로 보고 재시도한다


# LLM 단계명 -> 작업 종류 (사용량 기록의 task, 정책 presentation.md 6장). 모르는 단계명은 other
STAGE_TASKS = {
    "gate_label": "gate", "extract_requirements": "eligibility", "find_evidence": "eligibility",
    "write_cover_letter": "writer", "write_portfolio": "writer", "link_tasks": "writer",
}
MODEL_ROLES = ("judge", "writer")  # JUDGE(판정 모델): 게이트·자격요건, WRITER(생성 모델): 자소서·포트폴리오 (정책 presentation.md 1장)


def task_of(stage: str) -> str:
    return STAGE_TASKS.get(stage, "other")


class LLMError(Exception):
    """이 모듈이 내는 오류의 뿌리. 메시지에 키·프롬프트·모델 출력 원문을 넣지 않는다."""


class LLMStageError(LLMError):
    """단계 실패: 통신 실패, 재시도 소진, 응답이 스키마를 어김. 사용자에게는 정책 1-4의 안내 문구만 보인다."""


class LLMQuotaExceeded(LLMError):
    """일일 한도(RPD)나 프로젝트 quota 소진. 재시도하지 않고 멈춘다 (결제로 넘어가지 않는다, 규칙 04)."""


class LLMConfigError(LLMError):
    """키나 모델 ID가 없는 등 설정 문제. 호출하지 않는다."""


@dataclass(frozen=True)
class LLMRequest:
    stage: str                    # "extract_requirements" / "find_evidence"
    prompt: str
    schema: type[BaseModel]       # 응답 스키마(평탄한 Pydantic 클래스)
    job_id: str = ""
    prompt_version: str = "0"     # 프롬프트 파일 맨 위 version. 캐시 키에 들어간다
    contains_profile: bool = False  # 프로필이 든 요청은 기본으로 디스크 캐시에 남기지 않는다 (정책 16장)
    system: str | None = None     # 시스템 지시. 믿을 수 있는 규칙만 담는다. 외부에서 온 글은 prompt(사용자 메시지)에 넣는다


@dataclass(frozen=True)
class LLMResponse:
    data: dict
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False
    model: str = ""


class LLMClient(Protocol):
    """요건 추출과 근거 찾기가 쓰는 호출 창구."""

    model: str
    external: bool  # 입력이 외부 서비스로 나가는가. 나가면 프로필을 보내려면 호출하는 쪽의 명시적 허용이 필요하다 (정책 16장)

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        ...


def validate_data(request: LLMRequest, payload: Any) -> dict:
    """payload(dict 또는 JSON 문자열)를 요청의 스키마로 검증한다. 어기면 LLMStageError.

    메시지에는 어긴 위치와 종류만 적고 입력 값은 적지 않는다(모델 출력·프로필이 새지 않게).
    """
    try:
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        return request.schema.model_validate(payload).model_dump(mode="json")
    except json.JSONDecodeError:
        raise LLMStageError(f"{request.stage}({request.job_id}): 응답이 JSON이 아니다") from None
    except ValidationError as e:
        where = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}:{err['type']}" for err in e.errors()[:5])
        raise LLMStageError(f"{request.stage}({request.job_id}): 응답이 스키마를 어겼다 ({where})") from None


class BaseClient:
    """공통 뼈대: 구현체는 _complete만 만들고, 응답 검증은 여기서 한 번 한다."""

    model = ""
    external = False
    config_id = ""  # 생성 설정의 식별자. 같은 모델이라도 설정이 다르면 캐시를 나눈다

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        raw = self._complete(request)
        return LLMResponse(
            data=validate_data(request, raw.data), input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens, cached=False, model=self.model,
        )

    def _complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class FakeClient(BaseClient):
    """테스트용. 대본(응답 dict 또는 던질 예외)을 순서대로 낸다. 받은 요청은 requests에 쌓는다."""

    def __init__(self, script: list[Any], model: str = "fake-model"):
        self.script = list(script)
        self.model = model
        self.requests: list[LLMRequest] = []

    def _complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("FakeClient 대본이 바닥났다")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(data=item, input_tokens=10, output_tokens=5)


class MockClient(BaseClient):
    """fixture의 정답을 돌려준다. (stage, job_id)마다 응답 dict를 미리 받아 둔다. 네트워크를 쓰지 않는다."""

    model = "mock"

    def __init__(self, answers: Mapping[tuple[str, str], dict]):
        self.answers = dict(answers)

    def _complete(self, request: LLMRequest) -> LLMResponse:
        try:
            return LLMResponse(data=self.answers[(request.stage, request.job_id)])
        except KeyError:
            raise LLMStageError(f"mock에 정답이 없다: {request.stage}({request.job_id})") from None


# ---------------------------------------------------------------- 사용량 기록

class UsageLog:
    """호출 수·토큰·캐시 적중·모델 ID만 남긴다. 프롬프트와 응답 내용은 쓰지 않는다 (규칙 03, 정책 16장)."""

    def __init__(self, path: Path = USAGE_LOG_PATH, now: Callable[[], datetime] | None = None, run_id: str | None = None):
        self.path = Path(path)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.run_id = run_id or uuid.uuid4().hex[:8]  # 실행마다 따로 센다. 서로 다른 실행의 호출·토큰을 합산하지 않는다

    def pacific_date(self) -> str:
        return self._now().astimezone(PACIFIC).date().isoformat()

    def record(self, request: LLMRequest, *, model: str, input_tokens: int = 0, output_tokens: int = 0,
               cached: bool = False, ok: bool = True, error: str | None = None, config_id: str = "",
               model_actual: str | None = None, duration_ms: int | None = None) -> None:
        """model은 요청한 모델 ID, model_actual은 응답이 알려 준 실제 모델(모르면 None)이다. task는 단계명에서 정한다.

        duration_ms는 시도 한 번의 응답 시간이다. 한도·재시도 대기는 들어 있지 않다. 캐시 적중 줄에는 없다 (정책 presentation.md 6장).
        """
        row = {
            "ts": self._now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            "date_pt": self.pacific_date(), "run_id": self.run_id, "stage": request.stage, "task": task_of(request.stage), "job_id": request.job_id,
            "model": model, "model_actual": model_actual, "config": config_id, "prompt_version": request.prompt_version,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "cached": cached, "ok": ok,
        }
        if duration_ms is not None:
            row["duration_ms"] = duration_ms
        if error:  # 실패한 시도만. 오류 종류와 코드뿐이고 메시지는 쓰지 않는다(키·프롬프트가 섞일 수 있다)
            row["error"] = error
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _run_rows(self, stage: str | None = None, exclude_stage: str | None = None, task: str | None = None):
        """이 실행(run_id)의 기록 줄. stage를 주면 그 단계만, exclude_stage를 주면 그 단계를 뺀다. task를 주면 그 작업 종류만 본다
        (이전 기록에는 task 칸이 없어 단계명에서 다시 정한다)."""
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("run_id") != self.run_id:
                continue
            if (stage is not None and row.get("stage") != stage) or (exclude_stage is not None and row.get("stage") == exclude_stage):
                continue
            if task is not None and task_of(row.get("stage", "")) != task:
                continue
            yield row

    def run_totals(self, stage: str | None = None, exclude_stage: str | None = None, task: str | None = None) -> dict:
        """이 실행(run_id)의 호출 수(시도)·실패 수·캐시 적중 수·토큰. 다른 실행은 더하지 않는다.

        stage나 exclude_stage로 LLM 단계명(LLMRequest.stage)을 걸러 한 단계만, 또는 한 단계를 뺀 합계를 낼 수 있다 (data-contract 3-1).
        """
        totals = {"calls": 0, "failed": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
        for row in self._run_rows(stage, exclude_stage, task):
            if row.get("cached"):
                totals["cache_hits"] += 1
                continue
            totals["calls"] += 1
            totals["failed"] += not row.get("ok")
            totals["input_tokens"] += row.get("input_tokens", 0)
            totals["output_tokens"] += row.get("output_tokens", 0)
        return totals

    def timing(self, stage: str | None = None, exclude_stage: str | None = None, task: str | None = None) -> dict:
        """이 실행의 응답 시간: 시간이 기록된 시도 수(timed)와 합계·최대(밀리초). 캐시 적중 줄과 시간 칸이 없는 이전 줄은 세지 않는다."""
        durations = [row["duration_ms"] for row in self._run_rows(stage, exclude_stage, task)
                     if not row.get("cached") and isinstance(row.get("duration_ms"), int)]
        return {"timed": len(durations), "total_ms": sum(durations), "max_ms": max(durations, default=0)}

    def logical_calls(self, stage: str | None = None, exclude_stage: str | None = None, task: str | None = None) -> int:
        """이 실행의 논리 호출 수: (stage, job_id) 고유 쌍의 수. 재시도해도 하나이고 캐시 적중도 센다 (data-contract 3-1).

        한도 점검에서 막혀 첫 시도 전에 멈춘 요청은 기록이 없어 세지 않는다.
        """
        return len({(row.get("stage"), row.get("job_id")) for row in self._run_rows(stage, exclude_stage, task)})

    def models_used(self, task: str | None = None) -> dict[str, list[str]]:
        """이 실행에서 task(없으면 전체)의 호출이 쓴 모델: {"requested": [...], "actual": [...]}. 실제 모델은 응답이 알려 준 것만 센다."""
        rows = list(self._run_rows(task=task))
        return {"requested": sorted({r["model"] for r in rows if r.get("model")}),
                "actual": sorted({r["model_actual"] for r in rows if r.get("model_actual")})}

    def calls_today(self, model: str) -> int:
        """오늘(태평양 시간) 그 모델로 실제 보낸 호출 수. 캐시 적중은 세지 않고, 실패한 시도는 센다(한도는 시도마다 줄어든다)."""
        if not self.path.exists():
            return 0
        today = self.pacific_date()
        count = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("date_pt") == today and row.get("model") == model and not row.get("cached"):
                count += 1
        return count


def format_timing(timing: dict) -> str:
    """응답 시간 요약 문구. 모델 호출만 잰 값이라 한도 대기를 빼며, 사용자가 체감하는 전체 시간과 다르다 (정책 presentation.md 6장)."""
    if not timing["timed"]:
        return "응답 시간 기록 없음"
    return f"평균 응답 시간 {timing['total_ms'] / timing['timed'] / 1000:.1f}초(최대 {timing['max_ms'] / 1000:.1f}초, {timing['timed']}회 기록)"


# ---------------------------------------------------------------- 캐시

def cache_key(model: str, request: LLMRequest, config_id: str = "") -> str:
    """모델 ID·생성 설정·프롬프트 버전·단계·프롬프트·스키마의 해시. 키는 파일 이름이 되므로 내용이 노출되지 않는다."""
    schema = json.dumps(request.schema.model_json_schema(), sort_keys=True, ensure_ascii=False)
    parts = [model, config_id, request.prompt_version, request.stage, request.prompt, schema]
    if request.system:  # 시스템 지시가 없는 요청의 키는 이전과 같게 둔다
        parts.append(request.system)
    blob = json.dumps(parts, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CachedClient:
    """어떤 클라이언트든 감싸서 캐시와 사용량 기록을 더한다.

    공고만 든 요청(요건 추출)은 디스크(extract_dir)와 메모리에 캐시한다.
    프로필이 든 요청(근거 찾기)은 기본으로 메모리에만 두고, cache_profile=True일 때만 profile_dir에도 쓴다 (정책 16장).
    """

    def __init__(self, inner: LLMClient, *, usage: UsageLog | None = None, extract_dir: Path = EXTRACT_CACHE_DIR,
                 profile_dir: Path = PROFILE_CACHE_DIR, cache_profile: bool = False):
        self.inner = inner
        self.usage = usage
        self.extract_dir = Path(extract_dir)
        self.profile_dir = Path(profile_dir)
        self.cache_profile = cache_profile
        self._memory: dict[str, dict] = {}

    @property
    def model(self) -> str:
        return self.inner.model

    @property
    def external(self) -> bool:
        return getattr(self.inner, "external", True)  # 모르면 외부로 본다

    @property
    def config_id(self) -> str:
        return getattr(self.inner, "config_id", "")

    def _disk_dir(self, request: LLMRequest) -> Path | None:
        if not request.contains_profile:
            return self.extract_dir
        return self.profile_dir if self.cache_profile else None

    def _lookup(self, request: LLMRequest, key: str) -> dict | None:
        if key in self._memory:
            return self._memory[key]
        directory = self._disk_dir(request)
        if directory is None or not (directory / f"{key}.json").exists():
            return None
        try:
            data = json.loads((directory / f"{key}.json").read_text(encoding="utf-8"))
            return validate_data(request, data)
        except (OSError, json.JSONDecodeError, LLMStageError):
            return None  # 깨진 캐시는 없는 것으로 본다

    def _store(self, request: LLMRequest, key: str, data: dict) -> None:
        self._memory[key] = data
        directory = self._disk_dir(request)
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f"{key}.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(directory / f"{key}.json")

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        key = cache_key(self.model, request, self.config_id)
        hit = self._lookup(request, key)
        if hit is not None:
            if self.usage:
                self.usage.record(request, model=self.model, cached=True, config_id=self.config_id)
            return LLMResponse(data=hit, cached=True, model=self.model)
        response = self.inner.complete_json(request)
        self._store(request, key, response.data)
        return response


# ---------------------------------------------------------------- Gemini

@dataclass(frozen=True)
class GeminiSettings:
    api_key: str = ""
    model: str = ""
    rpm: int = DEFAULT_RPM
    rpd: int = DEFAULT_RPD

    def __repr__(self) -> str:  # 키가 로그에 나가지 않게
        return f"GeminiSettings(model={self.model!r}, rpm={self.rpm}, rpd={self.rpd}, api_key=<{'set' if self.api_key else 'unset'}>)"

    @classmethod
    def from_env(cls, env: Mapping[str, str], role: str | None = None) -> "GeminiSettings":
        """GEMINI_API_KEY만 읽는다. GOOGLE_API_KEY가 있으면 SDK가 우선할 수 있어 경고한다 (정책 16장, 플랜 D12).

        role이 "judge"나 "writer"이면 GEMINI_MODEL_<ROLE>, GEMINI_RPM_<ROLE>, GEMINI_RPD_<ROLE>를 먼저 읽고 없으면 GEMINI_MODEL, GEMINI_RPM, GEMINI_RPD로
        대체한다. role=None은 대체 없이 지금까지와 같다 (정책 presentation.md 1장).
        """
        if role is not None and role not in MODEL_ROLES:
            raise ValueError(f"role은 {MODEL_ROLES} 중 하나여야 한다: {role!r}")
        suffix = f"_{role.upper()}" if role else ""

        def pick(name: str) -> str:
            return ((env.get(name + suffix) if suffix else None) or env.get(name) or "").strip()

        if env.get("GOOGLE_API_KEY"):
            warnings.warn(
                "GOOGLE_API_KEY가 환경에 있다. 이 프로젝트는 GEMINI_API_KEY만 쓴다. "
                "결제가 연결됐을 수 있는 다른 키이면 지우거나 확인하라.", stacklevel=2,
            )
        key = (env.get("GEMINI_API_KEY") or "").strip()
        model = pick("GEMINI_MODEL")
        if not key:
            raise LLMConfigError("GEMINI_API_KEY가 없다. .env.example을 .env로 복사하고 AI Studio에서 받은 키를 넣어라")
        if not model:
            raise LLMConfigError(f"GEMINI_MODEL{suffix}이(가) 없다. 정확한 모델 ID를 .env에 적어라(latest·preview 별칭 금지)"
                                 + (". 비워 두면 GEMINI_MODEL을 쓴다" if suffix else ""))
        try:
            rpm = int(pick("GEMINI_RPM") or DEFAULT_RPM)
            rpd = int(pick("GEMINI_RPD") or DEFAULT_RPD)
        except ValueError:
            raise LLMConfigError("GEMINI_RPM, GEMINI_RPD는 정수여야 한다") from None
        if rpm < 1 or rpd < 1:
            raise LLMConfigError("GEMINI_RPM, GEMINI_RPD는 1 이상이어야 한다")
        return cls(api_key=key, model=model, rpm=rpm, rpd=rpd)


def read_env_file(path: Path = Path(".env")) -> dict[str, str]:
    """`KEY=VALUE` 줄만 읽는 최소 파서. 주석과 빈 줄은 건너뛴다. 값은 출력하지 않는다."""
    values: dict[str, str] = {}
    if not Path(path).exists():
        return values
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.split(" #")[0].strip().strip("\"'")
    return values


def load_env(path: Path = Path(".env")) -> dict[str, str]:
    """`.env` 값에 프로세스 환경변수를 덮어 쓴다(환경변수가 우선)."""
    return {**read_env_file(path), **os.environ}


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "<KEY>") if secret else text


def _retry_delay(details: Any) -> float | None:
    """서버가 알려 주는 대기 시간(RetryInfo.retryDelay, 예: "13s")."""
    for item in _error_items(details):
        delay = item.get("retryDelay")
        if isinstance(delay, str):
            m = re.fullmatch(r"(\d+(?:\.\d+)?)s", delay)
            if m:
                return float(m.group(1))
    return None


def _error_items(details: Any) -> list[dict]:
    if not isinstance(details, Mapping):
        return []
    err = details.get("error", details)
    items = err.get("details") if isinstance(err, Mapping) else None
    return [i for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []


def classify_429(details: Any) -> str:
    """429를 'transient'(분당 제한)와 'quota'(일일 한도·프로젝트 quota·구별 불가)로 가른다.

    QuotaFailure.violations의 quotaId에 PerMinute가 있고 PerDay가 없으면 일시적이다. 그 밖에는 모두 quota로 보고 멈춘다 (정책 13-1).
    """
    ids = []
    for item in _error_items(details):
        for v in item.get("violations", []) or []:
            if isinstance(v, Mapping):
                ids.append(f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}".lower())
    if not ids or any("perday" in i or "per_day" in i or "daily" in i for i in ids):
        return "quota"
    return "transient" if any("perminute" in i or "per_minute" in i for i in ids) else "quota"


def _is_network_error(e: BaseException) -> bool:
    if isinstance(e, (OSError, TimeoutError)):
        return True
    return type(e).__module__.split(".")[0] in ("httpx", "httpx2", "httpcore", "requests")


class GeminiClient(BaseClient):
    """Gemini Developer API 무료 티어. 자동 fallback은 없다 (정책 13-1).

    - 키는 설정에서 SDK에 명시적으로 넘긴다. 호출 사이에 최소 간격을 두고, 오늘 호출 수가 GEMINI_RPD에 닿으면 호출하지 않는다.
    - 일시 오류(5xx, 네트워크, 분당 제한 429)만 지수 대기로 MAX_RETRIES회 재시도한다. 일일 한도 429는 LLMQuotaExceeded.
    - generate, sleep, clock은 테스트에서 바꿔 끼운다.
    """

    external = True
    # 보내는 생성 설정. temperature·top_p·top_k는 공식 문서가 일부 모델에서 무시한다고 적고 있어 보내지 않는다(기본값). 바꾸면 config_id가 달라진다
    generation_config = {"response_mime_type": "application/json", "sampling": "default"}

    def __init__(self, settings: GeminiSettings, usage: UsageLog | None = None, *,
                 generate: Callable[[LLMRequest], tuple] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.model = settings.model
        self.usage = usage
        self._generate = generate or self._sdk_generate
        self._sleep = sleep
        self._clock = clock
        self._sdk_client = None
        self._last_call: float | None = None
        self.config_id = hashlib.sha256(json.dumps(self.generation_config, sort_keys=True).encode()).hexdigest()[:8]
        self.min_interval = 60.0 / settings.rpm + 1.0  # 5회면 13초. 한도에 닿아 429를 받는 대신 미리 늦춘다

    def remaining_calls(self) -> int:
        used = self.usage.calls_today(self.model) if self.usage else 0
        return max(self.settings.rpd - used, 0)

    def ensure_budget(self, needed_calls: int) -> None:
        """실행에 필요한 호출 수가 오늘 남은 한도를 넘으면 시작하기 전에 멈춘다 (D13)."""
        left = self.remaining_calls()
        if needed_calls > left:
            raise LLMQuotaExceeded(
                f"{self.model}: 오늘 남은 호출 {left}회, 필요한 호출 {needed_calls}회. "
                f"태평양 시간 자정(한국 시간 오후 5시경) 이후에 다시 하거나 다른 모델로 돌려라"
            )

    def _wait_for_interval(self) -> None:
        if self._last_call is not None:
            wait = self.min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _sdk_generate(self, request: LLMRequest) -> tuple[str, int, int, str | None]:
        from google import genai  # 호출 시점에만 가져온다
        from google.genai import types

        if self._sdk_client is None:
            self._sdk_client = genai.Client(
                api_key=self.settings.api_key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_SECONDS * 1000)  # 밀리초
            )
        config = types.GenerateContentConfig(
            response_mime_type=self.generation_config["response_mime_type"], response_schema=request.schema,
            system_instruction=request.system,  # None이면 보내지 않는다
        )
        r = self._sdk_client.models.generate_content(model=self.model, contents=request.prompt, config=config)
        usage = r.usage_metadata
        out = (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
        return r.text or "", getattr(usage, "prompt_token_count", 0) or 0, out, getattr(r, "model_version", None) or None

    def _fail(self, request: LLMRequest, what: str) -> LLMStageError:
        return LLMStageError(f"{request.stage}({request.job_id}): {what}")

    def _complete(self, request: LLMRequest) -> LLMResponse:
        attempt = 0
        while True:
            if self.remaining_calls() <= 0:
                raise LLMQuotaExceeded(f"{self.model}: 오늘 호출 한도(GEMINI_RPD={self.settings.rpd})에 닿았다")
            self._wait_for_interval()
            started = self._clock()  # 한도 대기 뒤부터 잰다. 재시도 대기는 다음 시도의 started 앞이라 섞이지 않는다
            try:
                result = self._generate(request)  # (text, 입력 토큰, 출력 토큰[, 응답이 알려 준 실제 모델])
                text, tokens_in, tokens_out = result[:3]
                model_actual = result[3] if len(result) > 3 else None
            except Exception as e:  # noqa: BLE001 - SDK 예외를 모두 감싸 키·원시 오류가 새지 않게 한다
                if self.usage:
                    code = getattr(e, "code", None)
                    self.usage.record(request, model=self.model, ok=False, config_id=self.config_id, duration_ms=self._elapsed_ms(started),
                                      error=type(e).__name__ + (f" {code}" if isinstance(code, int) else ""))
                wait = self._retry_wait(e, attempt, request)
                attempt += 1
                self._sleep(wait)
                continue
            if self.usage:
                self.usage.record(request, model=self.model, input_tokens=tokens_in, output_tokens=tokens_out, config_id=self.config_id,
                                  model_actual=model_actual, duration_ms=self._elapsed_ms(started))
            return LLMResponse(data=text, input_tokens=tokens_in, output_tokens=tokens_out, model=self.model)  # type: ignore[arg-type]

    def _elapsed_ms(self, started: float) -> int:
        return max(round((self._clock() - started) * 1000), 0)

    def _retry_wait(self, e: Exception, attempt: int, request: LLMRequest) -> float:
        """재시도할 오류면 대기 시간을, 아니면 예외를 낸다."""
        code = getattr(e, "code", None)
        details = getattr(e, "details", None)
        secret = self.settings.api_key
        label = f"{type(e).__name__}" + (f" {code}" if isinstance(code, int) else "")
        if code == 429:
            if classify_429(details) == "quota":
                raise LLMQuotaExceeded(f"{self.model}: 일일 한도나 quota 소진으로 보인다(429). 재시도하지 않는다") from None
        elif isinstance(code, int) and code < 500:
            raise self._fail(request, _redact(f"요청이 거부됐다 ({label})", secret)) from None
        elif not (isinstance(code, int) or _is_network_error(e)):
            raise self._fail(request, _redact(f"호출 중 알 수 없는 오류 ({label})", secret)) from None
        if attempt >= MAX_RETRIES:
            raise self._fail(request, _redact(f"재시도 {MAX_RETRIES}회 후에도 실패 ({label})", secret)) from None
        server_delay = _retry_delay(details)
        wait = server_delay if server_delay is not None else BACKOFF_BASE_SECONDS * 2 ** attempt
        return min(wait, MAX_WAIT_SECONDS)
