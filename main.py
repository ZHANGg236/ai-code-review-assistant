import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from folder_review import analyze_folder
from services.deepseek import DeepSeekStreamError, stream_review
from storage import (
    add_feedback,
    get_insights,
    get_memory_summary,
    get_review,
    init_db,
    list_reviews,
    save_review,
)

logger = logging.getLogger("uvicorn.error")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
load_dotenv(BASE_DIR / ".env")


def env_int(name: str, default: int) -> int:
    """读取整数环境变量，格式错误时回退到默认值。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 配置：全部通过环境变量读取，不要把密钥写进代码
# ---------------------------------------------------------------------------
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions").strip()
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

MAX_CODE_CHARS = env_int("MAX_CODE_CHARS", 20_000)
MAX_REQUEST_BYTES = MAX_CODE_CHARS * 6 + 8192
MAX_OUTPUT_TOKENS = env_int("MAX_OUTPUT_TOKENS", 4096)
QUICK_MAX_OUTPUT_TOKENS = env_int("QUICK_MAX_OUTPUT_TOKENS", 1000)
FIX_MAX_OUTPUT_TOKENS = env_int("FIX_MAX_OUTPUT_TOKENS", 4096)
FOLDER_MAX_FILES = env_int("FOLDER_MAX_FILES", 5)
FOLDER_MAX_FILE_CHARS = env_int("FOLDER_MAX_FILE_CHARS", 20_000)
FOLDER_MAX_OUTPUT_TOKENS = env_int("FOLDER_MAX_OUTPUT_TOKENS", 1200)
MAX_FOLDER_ZIP_BYTES = env_int("MAX_FOLDER_ZIP_BYTES", 30 * 1024 * 1024)
PROBLEM_MAX_OUTPUT_TOKENS = env_int("PROBLEM_MAX_OUTPUT_TOKENS", 2000)
TEST_REVIEW_MAX_OUTPUT_TOKENS = env_int("TEST_REVIEW_MAX_OUTPUT_TOKENS", 2000)
UPSTREAM_TIMEOUT = env_int("UPSTREAM_TIMEOUT_SECONDS", 120)
RATE_LIMIT_PER_MINUTE = env_int("RATE_LIMIT_PER_MINUTE", 30)

ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "").strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
TRUST_PROXY = os.getenv("TRUST_PROXY", "false").lower() in {"1", "true", "yes"}
SESSION_TTL = env_int("SESSION_TTL_SECONDS", 7 * 24 * 3600)

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

SESSION_COOKIE = "review_session"

ALLOWED_LANGUAGES = {
    "python",
    "javascript",
    "typescript",
    "java",
    "go",
    "c",
    "cpp",
    "csharp",
    "rust",
    "php",
    "ruby",
    "kotlin",
    "swift",
    "sql",
    "bash",
    "html",
    "css",
    "vue",
    "react",
    "other",
}

app = FastAPI(title="AI代码审查助手", version="0.3.0")

try:
    init_db()
except Exception:
    logger.exception("初始化审查历史数据库失败")

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

# 进程内会话和限流记录。单进程 MVP 足够；多进程/多实例时应换成 Redis。
_sessions: dict[str, float] = {}
_rate_buckets: dict[str, list[float]] = {}


# ---------------------------------------------------------------------------
# 会话与限流
# ---------------------------------------------------------------------------
def _cleanup_sessions() -> None:
    now = time.time()
    for token, expires in list(_sessions.items()):
        if expires <= now:
            _sessions.pop(token, None)


def _new_session() -> str:
    _cleanup_sessions()
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def _is_authenticated(request: Request) -> bool:
    if not ACCESS_PASSWORD:
        return True

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False

    expires = _sessions.get(token)
    if not expires or expires <= time.time():
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request: Request) -> None:
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="请先登录")


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request) -> None:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return

    now = time.time()
    ip = _client_ip(request)
    window = [stamp for stamp in _rate_buckets.get(ip, []) if now - stamp < 60]

    if len(window) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    window.append(now)
    _rate_buckets[ip] = window

    # 防止大量不同 IP 把字典撑大
    if len(_rate_buckets) > 1000:
        for key in list(_rate_buckets):
            _rate_buckets[key] = [
                stamp for stamp in _rate_buckets[key] if now - stamp < 60
            ]
            if not _rate_buckets[key]:
                _rate_buckets.pop(key, None)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    if request.method == "POST" and request.url.path in {"/api/review", "/api/login"}:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "请求体过大"},
                    )
            except ValueError:
                pass

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class CodeRequest(BaseModel):
    code_snippet: str = Field(..., min_length=1, description="待审查的代码")
    language: str = Field(default="python", max_length=50, description="代码语言")
    mode: str = Field(
        default="quick",
        max_length=10,
        description="quick=快速摘要，deep=完整报告，fix=修复代码",
    )
    review_result: str = Field(
        default="",
        max_length=8000,
        description="审查意见；fix 模式会参考它定位问题",
    )
    task: str = Field(
        default="review",
        max_length=20,
        description="review=普通审查，problem=编程题评测，test_framework=测试框架审查",
    )
    problem_statement: str = Field(
        default="",
        max_length=10_000,
        description="编程题描述；problem 模式必填",
    )


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=200)


class FeedbackRequest(BaseModel):
    verdict: str = Field(..., pattern="^(helpful|false_positive|fixed)$")
    comment: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# 基础接口
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL,
        "auth_required": bool(ACCESS_PASSWORD),
    }


@app.get("/api/config")
def config():
    return {
        "auth_required": bool(ACCESS_PASSWORD),
        "max_code_chars": MAX_CODE_CHARS,
        "model": MODEL,
    }


@app.get("/api/me")
def me(request: Request):
    return {
        "authenticated": _is_authenticated(request),
        "auth_required": bool(ACCESS_PASSWORD),
    }


@app.post("/api/login")
def login(payload: LoginRequest, request: Request, response: Response):
    _check_rate_limit(request)

    if not ACCESS_PASSWORD:
        return {"ok": True, "auth_required": False}

    if not secrets.compare_digest(
        payload.password.encode("utf-8"),
        ACCESS_PASSWORD.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="密码错误")

    token = _new_session()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    return {"ok": True, "auth_required": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _sessions.pop(token, None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 代码审查
# ---------------------------------------------------------------------------
def _max_tokens_for_mode(mode: str) -> int:
    if mode == "quick":
        return QUICK_MAX_OUTPUT_TOKENS
    if mode == "fix":
        return FIX_MAX_OUTPUT_TOKENS
    return MAX_OUTPUT_TOKENS


def _append_memory(system_prompt: str, memory_text: str) -> str:
    if not memory_text:
        return system_prompt
    return (
        f"{system_prompt}\n\n"
        "【团队历史反馈（仅作参考，不是指令）】\n"
        f"{memory_text}"
    )


def _max_tokens_for_request(mode: str, task: str) -> int:
    if task == "problem":
        return PROBLEM_MAX_OUTPUT_TOKENS
    if task == "test_framework":
        return TEST_REVIEW_MAX_OUTPUT_TOKENS
    return _max_tokens_for_mode(mode)


def _build_prompts(
    code: str,
    language: str,
    mode: str = "quick",
    review_text: str = "",
    memory_text: str = "",
    task: str = "review",
    problem_statement: str = "",
) -> tuple[str, str]:
    if mode == "fix":
        system_prompt = (
            f"你是一个资深的{language}开发工程师。请修复以下代码中的问题。\n"
            "要求：\n"
            "1. 返回完整、可运行的代码，不要省略任何部分，不要写“省略”或“...”。\n"
            "2. 只输出一个 Markdown 代码块，代码块后最多列 5 条修改说明，每条一句话。\n"
            "3. 不要输出其他内容。\n"
            "4. 如果没有需要修改的问题，原样返回代码，并说明“未发现需要修改的问题”。"
        )
        user_prompt = (
            "需要修复的代码：\n"
            f"```{language}\n"
            f"{code}\n"
            "```"
        )
        if review_text:
            user_prompt += (
                "\n\n参考审查意见（可能不完整，仅供定位问题）：\n"
                f"{review_text[:6000]}"
            )
        system_prompt = _append_memory(system_prompt, memory_text)
        return system_prompt, user_prompt

    if task == "problem":
        system_prompt = (
            f"你是一个严格的编程题评测员。给定题目和{language}代码，判断代码是否正确。\n"
            "要求：\n"
            "1. 先给结论：通过 / 不通过 / 部分通过。\n"
            "2. 对照题目要求逐条检查输入输出、边界、约束、复杂度。\n"
            "3. 至少给出 5 个测试用例（输入 + 期望输出），"
            "包括空输入、单元素、重复、极值、非法输入。\n"
            "4. 如果不通过，给出最小反例，说明为什么错。\n"
            "5. 最后给修复建议；不要只讲代码风格。\n"
            "6. 这是静态分析，不执行代码；不确定的地方标记“需要运行验证”。"
        )
        user_prompt = (
            f"题目描述：\n{problem_statement.strip()}\n\n"
            f"我的{language}代码：\n"
            f"```{language}\n{code}\n```"
        )
        system_prompt = _append_memory(system_prompt, memory_text)
        return system_prompt, user_prompt

    if task == "test_framework":
        system_prompt = (
            f"你是自动化测试框架/脚本审查专家。审查以下{language}测试代码。\n"
            "重点检查：\n"
            "1. 安全：硬编码密钥、命令注入、路径遍历、危险文件操作、敏感信息日志。\n"
            "2. 边界：空值、超时、重试、异常、资源清理、测试数据隔离。\n"
            "3. 稳定性：共享状态、执行顺序依赖、并行安全、硬编码 sleep、脆弱选择器。\n"
            "4. 有效性：断言是否真的验证业务，是否只检查“没报错”。\n"
            "输出：按严重级别列问题，给文件/函数位置、原因、修复建议；最多 8 条。\n"
            "不要只讲风格；没有把握的标记“需要确认”。"
        )
        user_prompt = (
            "测试框架/脚本代码：\n"
            f"```{language}\n{code}\n```"
        )
        system_prompt = _append_memory(system_prompt, memory_text)
        return system_prompt, user_prompt

    if mode == "quick":
        system_prompt = (
            f"你是一个资深的{language}开发工程师。请做一次快速代码审查，只关注最高风险。\n"
            "输出要求：\n"
            "1. 第一行必须是：> **结论：** <一句话说明最大风险，或是否可以上线>\n"
            "2. 然后输出：## 必须优先处理\n"
            "3. 最多列 3 条，按严重程度从高到低。每条格式：\n"
            "### 1. [严重级别] 问题标题\n"
            "- 位置：函数名或行号\n"
            "- 原因：一句话\n"
            "- 修复：一句话；必要时给不超过 5 行的代码\n"
            "4. 没有高置信度问题时，只输出：> **结论：** 未发现高置信度问题。\n"
            "5. 不要复述代码，不要写总结、客套话或完整报告。总长度控制在 600 字以内。"
        )
    else:
        system_prompt = (
            f"你是一个资深的{language}开发工程师。"
            "请审查以下代码，指出潜在的性能问题、安全漏洞或代码规范问题，并给出建议。"
            "请先给出一句话结论，再按严重程度从高到低列出问题。"
            "请使用 Markdown 格式输出。"
        )

    system_prompt = _append_memory(system_prompt, memory_text)
    user_prompt = (
        "这是需要审查的代码：\n"
        f"```{language}\n"
        f"{code}\n"
        "```"
    )
    return system_prompt, user_prompt


def _validate_review_request(
    payload: CodeRequest,
    request: Request,
) -> tuple[str, str, str, str, str]:
    """鉴权、限流和参数校验，两个审查接口共用。"""
    _check_rate_limit(request)
    _require_auth(request)

    code = payload.code_snippet
    if not code.strip():
        raise HTTPException(status_code=400, detail="代码不能为空")

    if len(code) > MAX_CODE_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"代码过长，最多 {MAX_CODE_CHARS} 个字符",
        )

    language = payload.language.strip().lower()
    if language not in ALLOWED_LANGUAGES:
        raise HTTPException(status_code=400, detail="不支持的代码语言")

    mode = payload.mode.strip().lower()
    if mode not in {"quick", "deep", "fix"}:
        raise HTTPException(status_code=400, detail="不支持的审查模式")

    task = payload.task.strip().lower()
    if task not in {"review", "problem", "test_framework"}:
        raise HTTPException(status_code=400, detail="不支持的审查场景")

    problem_statement = payload.problem_statement.strip()
    if task == "problem" and not problem_statement:
        raise HTTPException(status_code=400, detail="题目模式需要题目描述")

    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="服务端未配置 DEEPSEEK_API_KEY，请先设置环境变量并重启服务",
        )

    if not API_KEY.isascii() or any(ch.isspace() for ch in API_KEY):
        raise HTTPException(
            status_code=500,
            detail="DEEPSEEK_API_KEY 格式异常：不能包含中文、空格或换行",
        )

    return code, language, mode, task, problem_statement


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _upstream_error_message(status: int | None) -> str:
    if status is None:
        return "DeepSeek 流式返回错误"
    detail_map = {
        400: "DeepSeek 请求参数或模型名不正确",
        401: "DeepSeek API Key 无效或已过期",
        402: "DeepSeek 账户余额不足",
        429: "请求过于频繁，请稍后重试",
    }
    return detail_map.get(status, f"DeepSeek 接口返回错误（HTTP {status}）")


@app.post("/api/review")
def review_code(payload: CodeRequest, request: Request):
    code, language, mode, task, problem_statement = _validate_review_request(
        payload,
        request,
    )

    try:
        memory_text = get_memory_summary(language)
    except Exception:
        logger.exception("读取历史反馈失败")
        memory_text = ""

    system_prompt, user_prompt = _build_prompts(
        code,
        language,
        mode,
        payload.review_result,
        memory_text,
        task,
        problem_statement,
    )
    max_tokens = _max_tokens_for_request(mode, task)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }

    response = None
    try:
        response = requests.post(
            API_URL,
            headers=headers,
            json=data,
            timeout=(10, UPSTREAM_TIMEOUT),
        )
        response.raise_for_status()

        result = response.json()
        review_result = result["choices"][0]["message"]["content"]
        if not isinstance(review_result, str) or not review_result.strip():
            raise ValueError("empty review content")

        usage = result.get("usage") or {}
        logger.info(
            "review ok model=%s total_tokens=%s",
            MODEL,
            usage.get("total_tokens"),
        )
    except requests.Timeout as exc:
        logger.error("调用 DeepSeek 超时: %s", exc)
        raise HTTPException(
            status_code=504,
            detail="调用 AI 模型超时，请稍后重试",
        ) from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        body = exc.response.text[:500] if exc.response is not None else ""
        logger.error("DeepSeek 返回 HTTP %s: %s", status, body)

        detail_map = {
            400: "DeepSeek 请求参数或模型名不正确",
            401: "DeepSeek API Key 无效或已过期",
            402: "DeepSeek 账户余额不足",
            429: "请求过于频繁，请稍后重试",
        }
        raise HTTPException(
            status_code=502,
            detail=detail_map.get(
                status,
                f"DeepSeek 接口返回错误（HTTP {status}）",
            ),
        ) from exc
    except requests.JSONDecodeError as exc:
        raw = response.text[:500] if response is not None else ""
        logger.error("DeepSeek 返回的不是合法 JSON: %s", raw)
        raise HTTPException(status_code=502, detail="DeepSeek 返回格式异常") from exc
    except requests.RequestException as exc:
        logger.error("调用 DeepSeek 失败: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="无法连接 DeepSeek 服务，请稍后重试",
        ) from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raw = response.text[:500] if response is not None else ""
        logger.error("DeepSeek 返回结构异常，原始响应: %s", raw)
        raise HTTPException(status_code=502, detail="AI 模型返回格式异常") from exc

    try:
        review_id = save_review(
            language=language,
            mode=mode,
            code=code,
            review_text=review_result,
        )
    except Exception:
        logger.exception("保存审查历史失败")
        review_id = None

    return {
        "status": "success",
        "mode": mode,
        "task": task,
        "review_id": review_id,
        "message": "代码审查完成",
        "review_result": review_result,
        "usage": usage,
    }


@app.post("/api/review/stream")
async def review_code_stream(payload: CodeRequest, request: Request):
    code, language, mode, task, problem_statement = _validate_review_request(
        payload,
        request,
    )

    try:
        memory_text = get_memory_summary(language)
    except Exception:
        logger.exception("读取历史反馈失败")
        memory_text = ""

    system_prompt, user_prompt = _build_prompts(
        code,
        language,
        mode,
        payload.review_result,
        memory_text,
        task,
        problem_statement,
    )
    max_tokens = _max_tokens_for_request(mode, task)

    async def event_generator():
        sent_any = False
        collected_parts: list[str] = []
        yield _sse_event({"type": "start", "mode": mode, "task": task})

        try:
            async for chunk in stream_review(
                api_url=API_URL,
                api_key=API_KEY,
                model=MODEL,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                timeout=UPSTREAM_TIMEOUT,
            ):
                if await request.is_disconnected():
                    logger.info("client disconnected during review stream")
                    return
                sent_any = True
                collected_parts.append(chunk)
                yield _sse_event({"type": "delta", "content": chunk})
        except DeepSeekStreamError as exc:
            logger.error("DeepSeek stream error %s: %s", exc.status_code, exc)
            yield _sse_event(
                {"type": "error", "message": _upstream_error_message(exc.status_code)}
            )
            return
        except httpx.TimeoutException:
            logger.exception("DeepSeek stream timeout")
            yield _sse_event(
                {"type": "error", "message": "调用 AI 模型超时，请稍后重试"}
            )
            return
        except httpx.RequestError as exc:
            logger.error("DeepSeek stream request failed: %s", exc)
            yield _sse_event(
                {"type": "error", "message": "无法连接 DeepSeek 服务，请稍后重试"}
            )
            return
        except Exception:
            logger.exception("DeepSeek stream failed")
            yield _sse_event({"type": "error", "message": "AI 模型调用失败"})
            return

        if not sent_any:
            yield _sse_event({"type": "error", "message": "AI 模型没有返回内容"})
        else:
            review_id = None
            try:
                review_id = save_review(
                    language=language,
                    mode=mode,
                    code=code,
                    review_text="".join(collected_parts),
                )
            except Exception:
                logger.exception("保存审查历史失败")

            yield _sse_event({"type": "done", "review_id": review_id})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/analyze-folder")
def analyze_folder_endpoint(
    request: Request,
    file: UploadFile = File(...),
):
    _check_rate_limit(request)
    _require_auth(request)

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 文件")

    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="服务端未配置 DEEPSEEK_API_KEY",
        )

    if not API_KEY.isascii() or any(ch.isspace() for ch in API_KEY):
        raise HTTPException(
            status_code=500,
            detail="DEEPSEEK_API_KEY 格式异常：不能包含中文、空格或换行",
        )

    data = file.file.read(MAX_FOLDER_ZIP_BYTES + 1)
    if len(data) > MAX_FOLDER_ZIP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"zip 不能超过 {MAX_FOLDER_ZIP_BYTES // (1024 * 1024)} MB",
        )

    try:
        return analyze_folder(
            data,
            api_key=API_KEY,
            api_url=API_URL,
            model=MODEL,
            max_files=max(1, min(FOLDER_MAX_FILES, 10)),
            max_file_chars=FOLDER_MAX_FILE_CHARS,
            max_output_tokens=FOLDER_MAX_OUTPUT_TOKENS,
            timeout=UPSTREAM_TIMEOUT,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("文件夹分析失败")
        raise HTTPException(status_code=502, detail="文件夹分析失败")


@app.get("/api/reviews")
def api_reviews(request: Request, limit: int = 20):
    _require_auth(request)
    limit = max(1, min(limit, 50))
    return {"items": list_reviews(limit)}


@app.get("/api/reviews/{review_id}")
def api_review_detail(review_id: int, request: Request):
    _require_auth(request)
    review = get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return review


@app.post("/api/reviews/{review_id}/feedback")
def api_review_feedback(
    review_id: int,
    payload: FeedbackRequest,
    request: Request,
):
    _require_auth(request)
    if not add_feedback(review_id, payload.verdict, payload.comment):
        raise HTTPException(status_code=404, detail="审查记录不存在或反馈类型无效")
    return {"ok": True}


@app.get("/api/insights")
def api_insights(request: Request):
    _require_auth(request)
    return get_insights()


# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=500, detail="前端页面未安装")
    return FileResponse(index_file)
