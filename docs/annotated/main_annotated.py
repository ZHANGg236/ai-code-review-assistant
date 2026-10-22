# =============================================================================
# main.py 逐行注释学习版
#
# 说明：
#   1. 这是学习版，生产代码请看 D:\FastAPI\main.py。
#   2. 注释解释“这一行在做什么、为什么”。
#   3. 多行字符串内部不强行加 #，否则会改变字符串内容。
#   4. 空行不加注释，因为空行只是分隔代码块。
# =============================================================================

import json  # 标准库：把 Python 字典转成 JSON 字符串，SSE 事件要用
import logging  # 标准库：写日志，排查线上问题
import os  # 标准库：读取环境变量
import secrets  # 标准库：生成随机会话 token、安全比较密码
import time  # 标准库：时间戳、限流窗口

from pathlib import Path  # 标准库：用面向对象的方式处理文件路径

import httpx  # 第三方：异步 HTTP 客户端，流式调用 DeepSeek 用
import requests  # 第三方：同步 HTTP 客户端，普通审查调用 DeepSeek 用

from fastapi import FastAPI, HTTPException, Request, Response  # FastAPI 核心对象
from fastapi.middleware.cors import CORSMiddleware  # 跨域中间件，前后端分离时才用
from fastapi.responses import (  # 各种响应类型
    FileResponse,  # 返回文件，例如 index.html
    JSONResponse,  # 直接返回 JSON，例如 413
    StreamingResponse,  # 返回流式响应，SSE 用
)
from fastapi.staticfiles import StaticFiles  # 托管 /static 下的 CSS/JS
from pydantic import BaseModel, Field  # 请求体模型和字段校验

from services.deepseek import (  # 自己写的服务层
    DeepSeekStreamError,  # 上游流式调用错误
    stream_review,  # 流式调用 DeepSeek 的函数
)

logger = logging.getLogger("uvicorn.error")  # 复用 uvicorn 日志器，输出到终端

BASE_DIR = Path(__file__).resolve().parent  # 当前 main.py 所在目录，也就是 D:\FastAPI
STATIC_DIR = BASE_DIR / "static"  # 静态文件目录，存放 index.html、app.js、style.css


def env_int(name: str, default: int) -> int:  # 定义函数：读取整数环境变量
    """读取整数环境变量，格式错误时回退到默认值。"""  # 文档字符串，说明函数用途
    try:  # 尝试执行下面的转换
        return int(os.getenv(name, str(default)))  # 读环境变量并转成 int
    except (TypeError, ValueError):  # 如果环境变量不是合法整数
        return default  # 回退到默认值，避免服务启动失败


# ---------------------------------------------------------------------------
# 配置：全部通过环境变量读取，不要把密钥写进代码
# ---------------------------------------------------------------------------
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()  # 读取 DeepSeek Key，并去掉首尾空格
API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions").strip()  # 读取接口地址
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()  # 读取模型名

MAX_CODE_CHARS = env_int("MAX_CODE_CHARS", 20_000)  # 单次提交代码的最大字符数
MAX_REQUEST_BYTES = MAX_CODE_CHARS * 6 + 8192  # 请求体字节上限，给中文和 JSON 结构留余量
MAX_OUTPUT_TOKENS = env_int("MAX_OUTPUT_TOKENS", 4096)  # 深度模式最大输出 token 数
QUICK_MAX_OUTPUT_TOKENS = env_int("QUICK_MAX_OUTPUT_TOKENS", 1000)  # 快速模式最大输出 token 数
FIX_MAX_OUTPUT_TOKENS = env_int("FIX_MAX_OUTPUT_TOKENS", 4096)  # 修复模式最大输出 token 数
UPSTREAM_TIMEOUT = env_int("UPSTREAM_TIMEOUT_SECONDS", 120)  # 调用 DeepSeek 的读取超时
RATE_LIMIT_PER_MINUTE = env_int("RATE_LIMIT_PER_MINUTE", 30)  # 每 IP 每分钟请求上限

ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "").strip()  # 访问密码；为空表示不启用登录
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}  # 是否只通过 HTTPS 发送 Cookie
TRUST_PROXY = os.getenv("TRUST_PROXY", "false").lower() in {"1", "true", "yes"}  # 是否信任 X-Forwarded-For
SESSION_TTL = env_int("SESSION_TTL_SECONDS", 7 * 24 * 3600)  # 登录有效期，默认 7 天

CORS_ORIGINS = [  # 允许跨域的来源列表
    origin.strip()  # 去掉每个来源两边的空格
    for origin in os.getenv("CORS_ORIGINS", "").split(",")  # 用逗号分隔环境变量
    if origin.strip()  # 过滤空字符串
]

SESSION_COOKIE = "review_session"  # 登录 Cookie 的名字

ALLOWED_LANGUAGES = {  # 允许的代码语言白名单
    "python",  # Python
    "javascript",  # JavaScript
    "typescript",  # TypeScript
    "java",  # Java
    "go",  # Go
    "c",  # C
    "cpp",  # C++
    "csharp",  # C#
    "rust",  # Rust
    "php",  # PHP
    "ruby",  # Ruby
    "kotlin",  # Kotlin
    "swift",  # Swift
    "sql",  # SQL
    "bash",  # Bash
    "html",  # HTML
    "css",  # CSS
    "vue",  # Vue
    "react",  # React
    "other",  # 其他
}


app = FastAPI(title="AI代码审查助手", version="0.2.0")  # 创建 FastAPI 应用对象

if CORS_ORIGINS:  # 只有配置了跨域来源才启用 CORS
    app.add_middleware(  # 给应用添加中间件
        CORSMiddleware,  # 使用 FastAPI 自带的跨域中间件
        allow_origins=CORS_ORIGINS,  # 允许的来源
        allow_credentials=True,  # 允许携带 Cookie
        allow_methods=["GET", "POST", "OPTIONS"],  # 允许的 HTTP 方法
        allow_headers=["*"],  # 允许所有请求头
    )

# 进程内会话和限流记录。单进程 MVP 足够；多进程/多实例时应换成 Redis。
_sessions: dict[str, float] = {}  # 会话 token -> 过期时间戳
_rate_buckets: dict[str, list[float]] = {}  # IP -> 最近 60 秒的请求时间戳列表


# ---------------------------------------------------------------------------
# 会话与限流
# ---------------------------------------------------------------------------
def _cleanup_sessions() -> None:  # 清理过期会话
    """删除过期的会话，避免字典越来越大。"""  # 说明用途
    now = time.time()  # 当前时间戳
    for token, expires in list(_sessions.items()):  # 遍历副本，避免边遍历边删除
        if expires <= now:  # 如果已经过期
            _sessions.pop(token, None)  # 从字典中删除


def _new_session() -> str:  # 创建一个新会话
    """生成随机 token，并记录过期时间。"""  # 说明用途
    _cleanup_sessions()  # 先清理旧会话
    token = secrets.token_urlsafe(32)  # 生成 32 字节的随机 URL 安全 token
    _sessions[token] = time.time() + SESSION_TTL  # 记录这个 token 的过期时间
    return token  # 返回 token，稍后写入 Cookie


def _is_authenticated(request: Request) -> bool:  # 判断当前请求是否已登录
    """没设密码时直接放行；设了密码就检查 Cookie。"""  # 说明用途
    if not ACCESS_PASSWORD:  # 如果没有设置访问密码
        return True  # 本机开发模式，直接放行

    token = request.cookies.get(SESSION_COOKIE)  # 从 Cookie 里取会话 token
    if not token:  # 如果没有 token
        return False  # 未登录

    expires = _sessions.get(token)  # 查这个 token 的过期时间
    if not expires or expires <= time.time():  # 如果 token 不存在或已过期
        _sessions.pop(token, None)  # 顺手删除过期 token
        return False  # 未登录
    return True  # 已登录


def _require_auth(request: Request) -> None:  # 要求必须登录
    """未登录时抛 401。"""  # 说明用途
    if not _is_authenticated(request):  # 如果未登录
        raise HTTPException(status_code=401, detail="请先登录")  # 抛出 401


def _client_ip(request: Request) -> str:  # 获取客户端 IP
    """拿客户端 IP。在 Caddy/Nginx 后面时要信任 X-Forwarded-For。"""  # 说明用途
    if TRUST_PROXY:  # 如果配置了信任代理
        forwarded = request.headers.get("x-forwarded-for", "")  # 取代理转发头
        if forwarded:  # 如果头存在
            return forwarded.split(",")[0].strip()  # 取第一个 IP，去掉空格
    return request.client.host if request.client else "unknown"  # 否则用直连 IP


def _check_rate_limit(request: Request) -> None:  # 检查是否触发限流
    """滑动窗口限流：每个 IP 最近 60 秒最多 N 次。"""  # 说明用途
    if RATE_LIMIT_PER_MINUTE <= 0:  # 如果配置为 0 或负数
        return  # 关闭限流

    now = time.time()  # 当前时间
    ip = _client_ip(request)  # 当前请求的 IP

    window = [  # 只保留最近 60 秒的请求时间戳
        stamp for stamp in _rate_buckets.get(ip, [])  # 遍历这个 IP 的历史记录
        if now - stamp < 60  # 只要 60 秒内的
    ]

    if len(window) >= RATE_LIMIT_PER_MINUTE:  # 如果 60 秒内请求次数达到上限
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")  # 返回 429

    window.append(now)  # 把本次请求时间加进去
    _rate_buckets[ip] = window  # 写回字典

    # 防止大量不同 IP 把字典撑大
    if len(_rate_buckets) > 1000:  # 如果 IP 数量超过 1000
        for key in list(_rate_buckets):  # 遍历所有 IP
            _rate_buckets[key] = [  # 清理每个 IP 的过期记录
                stamp for stamp in _rate_buckets[key]  # 遍历时间戳
                if now - stamp < 60  # 只保留 60 秒内的
            ]
            if not _rate_buckets[key]:  # 如果清理后为空
                _rate_buckets.pop(key, None)  # 删除这个 IP


@app.middleware("http")  # 注册一个 HTTP 中间件
async def add_security_headers(request: Request, call_next):  # 中间件函数
    # 1. 超大请求体直接拒绝，避免先解析再发现太大
    if request.method == "POST" and request.url.path in {  # 只检查这两个 POST 接口
        "/api/review",  # 普通审查
        "/api/login",  # 登录
    }:
        content_length = request.headers.get("content-length")  # 读取请求体长度
        if content_length:  # 如果请求头里有长度
            try:  # 尝试转成整数
                if int(content_length) > MAX_REQUEST_BYTES:  # 如果超过上限
                    return JSONResponse(  # 直接返回 413
                        status_code=413,  # 请求体过大
                        content={"detail": "请求体过大"},  # 错误信息
                    )
            except ValueError:  # 如果长度不是合法数字
                pass  # 忽略，交给后面的逻辑

    response = await call_next(request)  # 把请求交给后续路由处理

    # 2. 基础安全响应头
    response.headers["X-Content-Type-Options"] = "nosniff"  # 禁止浏览器猜 MIME 类型
    response.headers["X-Frame-Options"] = "DENY"  # 禁止被 iframe 嵌入，防点击劫持
    response.headers["Referrer-Policy"] = "no-referrer"  # 不发送 Referer

    # 3. API 响应不缓存，避免审查结果被缓存
    if request.url.path.startswith("/api/"):  # 如果是 API 路径
        response.headers["Cache-Control"] = "no-store"  # 禁止缓存

    return response  # 返回响应


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class CodeRequest(BaseModel):  # 代码审查请求体
    code_snippet: str = Field(..., min_length=1, description="待审查的代码")  # 必填，至少 1 个字符
    language: str = Field(default="python", max_length=50, description="代码语言")  # 可选，默认 python
    mode: str = Field(default="quick", max_length=10, description="quick=快速摘要，deep=完整报告，fix=修复代码")  # 审查模式
    review_result: str = Field(default="", max_length=8000, description="审查意见；fix 模式会参考它")  # 上一轮审查结果


class LoginRequest(BaseModel):  # 登录请求体
    password: str = Field(..., min_length=1, max_length=200)  # 必填，1 到 200 个字符


# ---------------------------------------------------------------------------
# 基础接口
# ---------------------------------------------------------------------------
@app.get("/health")  # 注册 GET /health
def health():  # 健康检查函数
    return {  # 返回一个字典，FastAPI 会自动转 JSON
        "status": "ok",  # 服务状态
        "model": MODEL,  # 当前模型名
        "auth_required": bool(ACCESS_PASSWORD),  # 是否启用了访问密码
    }


@app.get("/api/config")  # 注册 GET /api/config
def config():  # 前端配置接口
    return {  # 返回前端需要的配置
        "auth_required": bool(ACCESS_PASSWORD),  # 是否需要登录
        "max_code_chars": MAX_CODE_CHARS,  # 代码最大长度
        "model": MODEL,  # 模型名
    }


@app.get("/api/me")  # 注册 GET /api/me
def me(request: Request):  # 当前登录状态
    return {  # 返回登录状态
        "authenticated": _is_authenticated(request),  # 是否已登录
        "auth_required": bool(ACCESS_PASSWORD),  # 是否启用了密码
    }


@app.post("/api/login")  # 注册 POST /api/login
def login(payload: LoginRequest, request: Request, response: Response):  # 登录函数
    _check_rate_limit(request)  # 登录接口也要限流，防止暴力破解

    if not ACCESS_PASSWORD:  # 如果没设置密码
        return {"ok": True, "auth_required": False}  # 直接返回成功

    if not secrets.compare_digest(  # 安全比较密码，减少时序攻击
        payload.password.encode("utf-8"),  # 用户输入的密码转 bytes
        ACCESS_PASSWORD.encode("utf-8"),  # 服务端配置的密码转 bytes
    ):
        raise HTTPException(status_code=401, detail="密码错误")  # 密码不对，返回 401

    token = _new_session()  # 创建新的会话 token

    response.set_cookie(  # 把 token 写入 Cookie
        key=SESSION_COOKIE,  # Cookie 名字
        value=token,  # 会话 token
        max_age=SESSION_TTL,  # 有效期
        httponly=True,  # JS 读不到，降低 XSS 窃取风险
        samesite="lax",  # 降低 CSRF 风险
        secure=COOKIE_SECURE,  # HTTPS 时才发送
        path="/",  # 整个站点都携带
    )
    return {"ok": True, "auth_required": True}  # 登录成功


@app.post("/api/logout")  # 注册 POST /api/logout
def logout(request: Request, response: Response):  # 退出登录
    token = request.cookies.get(SESSION_COOKIE)  # 取出当前 Cookie
    if token:  # 如果存在
        _sessions.pop(token, None)  # 从服务端会话表删除
    response.delete_cookie(SESSION_COOKIE, path="/")  # 让浏览器删除 Cookie
    return {"ok": True}  # 返回成功


# ---------------------------------------------------------------------------
# 代码审查
# ---------------------------------------------------------------------------
def _max_tokens_for_mode(mode: str) -> int:  # 按模式选择最大输出
    if mode == "quick":  # 快速模式
        return QUICK_MAX_OUTPUT_TOKENS  # 用较小的上限
    if mode == "fix":  # 修复模式
        return FIX_MAX_OUTPUT_TOKENS  # 修复代码可能较长
    return MAX_OUTPUT_TOKENS  # 深度模式


def _build_prompts(  # 组装 Prompt
    code: str,
    language: str,
    mode: str = "quick",
    review_text: str = "",
) -> tuple[str, str]:
    if mode == "quick":  # 快速模式：只列结论和 Top 3
        system_prompt = (  # 快速模式系统提示词
            f"你是一个资深的{language}开发工程师。请做一次快速代码审查，只关注最高风险。\n"  # 角色和任务
            "输出要求：\n"  # 输出规则
            "1. 第一行必须是：> **结论：** <一句话说明最大风险，或是否可以上线>\n"  # 先给结论
            "2. 然后输出：## 必须优先处理\n"  # 固定标题
            "3. 最多列 3 条，按严重程度从高到低。每条格式：\n"  # 限制数量
            "### 1. [严重级别] 问题标题\n"  # 标题格式
            "- 位置：函数名或行号\n"  # 位置
            "- 原因：一句话\n"  # 原因
            "- 修复：一句话；必要时给不超过 5 行的代码\n"  # 修复
            "4. 没有高置信度问题时，只输出：> **结论：** 未发现高置信度问题。\n"  # 无问题时的输出
            "5. 不要复述代码，不要写总结、客套话或完整报告。总长度控制在 600 字以内。"  # 控制长度
        )
    elif mode == "fix":  # 修复模式：返回完整修正代码
        system_prompt = (  # 修复模式系统提示词
            f"你是一个资深的{language}开发工程师。请修复以下代码中的问题。\n"  # 角色和任务
            "要求：\n"  # 输出规则
            "1. 返回完整、可运行的代码，不要省略任何部分，不要写“省略”或“...”。\n"  # 完整代码
            "2. 只输出一个 Markdown 代码块，代码块后最多列 5 条修改说明，每条一句话。\n"  # 代码块
            "3. 不要输出其他内容。\n"  # 不要客套话
            "4. 如果没有需要修改的问题，原样返回代码，并说明“未发现需要修改的问题”。"  # 无问题时的输出
        )
        user_prompt = (  # 修复模式的用户提示词
            "需要修复的代码：\n"  # 引导语
            f"```{language}\n"  # 代码围栏开始
            f"{code}\n"  # 用户代码
            "```"  # 代码围栏结束
        )
        if review_text:  # 如果前端传了上一轮审查意见
            user_prompt += (  # 追加审查意见，帮助定位问题
                "\n\n参考审查意见（可能不完整，仅供定位问题）：\n"
                f"{review_text[:6000]}"  # 最多 6000 字，控制输入
            )
        return system_prompt, user_prompt  # 修复模式提前返回
    else:  # 深度模式：完整报告
        system_prompt = (  # 深度模式系统提示词
            f"你是一个资深的{language}开发工程师。"  # 角色
            "请审查以下代码，指出潜在的性能问题、安全漏洞或代码规范问题，并给出建议。"  # 任务
            "请先给出一句话结论，再按严重程度从高到低列出问题。"  # 先结论再细节
            "请使用 Markdown 格式输出。"  # 输出格式
        )
    user_prompt = (  # 用户提示词：放具体代码
        "这是需要审查的代码：\n"  # 引导语
        f"```{language}\n"  # Markdown 代码围栏开始
        f"{code}\n"  # 用户代码
        "```"  # 代码围栏结束
    )
    return system_prompt, user_prompt  # 返回两个 Prompt


def _validate_review_request(payload: CodeRequest, request: Request) -> tuple[str, str, str]:  # 两个审查接口共用的校验
    """鉴权、限流和参数校验，两个审查接口共用。"""  # 说明用途
    _check_rate_limit(request)  # 先限流，避免被刷

    _require_auth(request)  # 再鉴权，未登录返回 401

    code = payload.code_snippet  # 取出代码，保留原始内容

    if not code.strip():  # 如果去掉空白后为空
        raise HTTPException(status_code=400, detail="代码不能为空")  # 返回 400

    if len(code) > MAX_CODE_CHARS:  # 如果超过长度上限
        raise HTTPException(  # 返回 413
            status_code=413,  # 请求实体过大
            detail=f"代码过长，最多 {MAX_CODE_CHARS} 个字符",  # 错误信息
        )

    language = payload.language.strip().lower()  # 语言统一转小写并去空格
    if language not in ALLOWED_LANGUAGES:  # 如果不在白名单
        raise HTTPException(status_code=400, detail="不支持的代码语言")  # 返回 400

    mode = payload.mode.strip().lower()  # 模式统一转小写并去空格
    if mode not in {"quick", "deep", "fix"}:  # 只允许快速/深度/修复三种模式
        raise HTTPException(status_code=400, detail="不支持的审查模式")  # 返回 400

    if not API_KEY:  # 如果服务端没有配置 Key
        raise HTTPException(  # 返回 500
            status_code=500,  # 服务端配置错误
            detail="服务端未配置 DEEPSEEK_API_KEY，请先设置环境变量并重启服务",  # 提示
        )

    if not API_KEY.isascii() or any(ch.isspace() for ch in API_KEY):  # Key 必须是 ASCII 且不能有空白
        raise HTTPException(  # 返回 500
            status_code=500,  # 服务端配置错误
            detail="DEEPSEEK_API_KEY 格式异常：不能包含中文、空格或换行",  # 提示
        )

    return code, language, mode  # 返回校验后的代码、语言和模式


def _sse_event(payload: dict) -> str:  # 把字典转成 SSE 事件字符串
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"  # SSE 格式：data: JSON + 空行


def _upstream_error_message(status: int | None) -> str:  # 把上游状态码转成友好提示
    if status is None:  # 如果没有状态码
        return "DeepSeek 流式返回错误"  # 通用错误
    detail_map = {  # 状态码映射表
        400: "DeepSeek 请求参数或模型名不正确",  # 参数/模型错误
        401: "DeepSeek API Key 无效或已过期",  # Key 无效
        402: "DeepSeek 账户余额不足",  # 余额不足
        429: "请求过于频繁，请稍后重试",  # 上游限流
    }
    return detail_map.get(status, f"DeepSeek 接口返回错误（HTTP {status}）")  # 返回映射或默认提示


@app.post("/api/review")  # 注册普通审查接口
def review_code(payload: CodeRequest, request: Request):  # 普通审查函数
    code, language, mode = _validate_review_request(payload, request)  # 鉴权/限流/校验
    system_prompt, user_prompt = _build_prompts(code, language, mode, payload.review_result)  # 按模式组装 Prompt
    max_tokens = _max_tokens_for_mode(mode)  # 按模式选输出上限

    headers = {  # 请求 DeepSeek 的 HTTP 头
        "Authorization": f"Bearer {API_KEY}",  # 用 Bearer Token 认证
        "Content-Type": "application/json",  # 请求体是 JSON
    }
    data = {  # 请求体
        "model": MODEL,  # 模型名
        "messages": [  # 对话消息列表
            {"role": "system", "content": system_prompt},  # 系统消息
            {"role": "user", "content": user_prompt},  # 用户消息
        ],
        "temperature": 0.3,  # 温度，越低越稳定
        "max_tokens": max_tokens,  # 按模式选择的最大输出 token
    }

    response = None  # 先占位，方便异常处理里引用
    try:  # 开始调用 DeepSeek
        response = requests.post(  # 同步 POST 请求
            API_URL,  # DeepSeek 接口地址
            headers=headers,  # 请求头
            json=data,  # 请求体
            timeout=(10, UPSTREAM_TIMEOUT),  # 连接 10 秒，读取 UPSTREAM_TIMEOUT 秒
        )
        response.raise_for_status()  # 如果状态码是 4xx/5xx，抛 HTTPError

        result = response.json()  # 把响应体解析成 JSON
        review_result = result["choices"][0]["message"]["content"]  # 取出审查文本
        if not isinstance(review_result, str) or not review_result.strip():  # 如果内容为空
            raise ValueError("empty review content")  # 主动抛异常

        usage = result.get("usage") or {}  # 取出 token 用量
        logger.info(  # 记录日志
            "review ok model=%s total_tokens=%s",  # 日志格式
            MODEL,  # 模型名
            usage.get("total_tokens"),  # 总 token
        )
    except requests.Timeout as exc:  # 超时
        logger.error("调用 DeepSeek 超时: %s", exc)  # 记录日志
        raise HTTPException(  # 返回 504
            status_code=504,  # 网关超时
            detail="调用 AI 模型超时，请稍后重试",  # 提示
        ) from exc
    except requests.HTTPError as exc:  # 上游返回 4xx/5xx
        status = exc.response.status_code if exc.response is not None else None  # 取状态码
        body = exc.response.text[:500] if exc.response is not None else ""  # 取响应体前 500 字
        logger.error("DeepSeek 返回 HTTP %s: %s", status, body)  # 记录日志

        detail_map = {  # 状态码映射
            400: "DeepSeek 请求参数或模型名不正确",  # 参数/模型错误
            401: "DeepSeek API Key 无效或已过期",  # Key 无效
            402: "DeepSeek 账户余额不足",  # 余额不足
            429: "请求过于频繁，请稍后重试",  # 上游限流
        }
        raise HTTPException(  # 返回 502
            status_code=502,  # 上游错误
            detail=detail_map.get(  # 友好提示
                status,  # 状态码
                f"DeepSeek 接口返回错误（HTTP {status}）",  # 默认提示
            ),
        ) from exc
    except requests.JSONDecodeError as exc:  # 返回的不是合法 JSON
        raw = response.text[:500] if response is not None else ""  # 取原始响应
        logger.error("DeepSeek 返回的不是合法 JSON: %s", raw)  # 记录日志
        raise HTTPException(status_code=502, detail="DeepSeek 返回格式异常") from exc  # 返回 502
    except requests.RequestException as exc:  # 网络错误、DNS 错误等
        logger.error("调用 DeepSeek 失败: %s", exc)  # 记录日志
        raise HTTPException(  # 返回 502
            status_code=502,  # 上游/网络错误
            detail="无法连接 DeepSeek 服务，请稍后重试",  # 提示
        ) from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:  # 返回结构不符合预期
        raw = response.text[:500] if response is not None else ""  # 取原始响应
        logger.error("DeepSeek 返回结构异常，原始响应: %s", raw)  # 记录日志
        raise HTTPException(status_code=502, detail="AI 模型返回格式异常") from exc  # 返回 502

    return {  # 返回成功响应
        "status": "success",  # 状态
        "mode": mode,  # 本次使用的模式
        "message": "代码审查完成",  # 提示
        "review_result": review_result,  # 审查结果
        "usage": usage,  # token 用量
    }


@app.post("/api/review/stream")  # 注册流式审查接口
async def review_code_stream(payload: CodeRequest, request: Request):  # 流式审查函数
    code, language, mode = _validate_review_request(payload, request)  # 鉴权/限流/校验
    system_prompt, user_prompt = _build_prompts(code, language, mode, payload.review_result)  # 按模式组装 Prompt
    max_tokens = QUICK_MAX_OUTPUT_TOKENS if mode == "quick" else MAX_OUTPUT_TOKENS  # 按模式选上限

    async def event_generator():  # 异步生成器：一段一段产出 SSE
        sent_any = False  # 是否已经产出过内容
        yield _sse_event({"type": "start", "mode": mode})  # 先发 start 事件

        try:  # 开始调用 DeepSeek 流式接口
            async for chunk in stream_review(  # 异步遍历模型输出
                api_url=API_URL,  # 接口地址
                api_key=API_KEY,  # Key
                model=MODEL,  # 模型
                system_prompt=system_prompt,  # 系统提示词
                user_prompt=user_prompt,  # 用户提示词
                max_tokens=max_tokens,  # 按模式选择的最大输出
                timeout=UPSTREAM_TIMEOUT,  # 超时
            ):
                if await request.is_disconnected():  # 如果浏览器断开
                    logger.info("client disconnected during review stream")  # 记录日志
                    return  # 停止生成，节省 token
                sent_any = True  # 标记已经产出内容
                yield _sse_event({"type": "delta", "content": chunk})  # 发送一段文本
        except DeepSeekStreamError as exc:  # 上游返回错误
            logger.error("DeepSeek stream error %s: %s", exc.status_code, exc)  # 记录日志
            yield _sse_event(  # 发送 error 事件
                {"type": "error", "message": _upstream_error_message(exc.status_code)}
            )
            return  # 结束
        except httpx.TimeoutException:  # 超时
            logger.exception("DeepSeek stream timeout")  # 记录日志
            yield _sse_event(  # 发送 error 事件
                {"type": "error", "message": "调用 AI 模型超时，请稍后重试"}
            )
            return  # 结束
        except httpx.RequestError as exc:  # 网络错误
            logger.error("DeepSeek stream request failed: %s", exc)  # 记录日志
            yield _sse_event(  # 发送 error 事件
                {"type": "error", "message": "无法连接 DeepSeek 服务，请稍后重试"}
            )
            return  # 结束
        except Exception:  # 其他未知错误
            logger.exception("DeepSeek stream failed")  # 记录日志
            yield _sse_event({"type": "error", "message": "AI 模型调用失败"})  # 发送 error 事件
            return  # 结束

        if not sent_any:  # 如果模型没有返回任何内容
            yield _sse_event({"type": "error", "message": "AI 模型没有返回内容"})  # 发送 error
        else:  # 如果正常返回过内容
            yield _sse_event({"type": "done"})  # 发送 done 事件

    return StreamingResponse(  # 返回流式响应
        event_generator(),  # 传入异步生成器
        media_type="text/event-stream",  # SSE 的 MIME 类型
        headers={  # 额外响应头
            "Cache-Control": "no-cache",  # 不缓存
            "Connection": "keep-alive",  # 保持连接
            "X-Accel-Buffering": "no",  # 告诉 Nginx 不要缓冲
        },
    )


# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------
if STATIC_DIR.is_dir():  # 如果 static 目录存在
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")  # 托管静态文件


@app.get("/", include_in_schema=False)  # 注册首页，不在 Swagger 里显示
def index():  # 首页函数
    index_file = STATIC_DIR / "index.html"  # 拼接 index.html 路径
    if not index_file.is_file():  # 如果文件不存在
        raise HTTPException(status_code=500, detail="前端页面未安装")  # 返回 500
    return FileResponse(index_file)  # 返回 HTML 文件
