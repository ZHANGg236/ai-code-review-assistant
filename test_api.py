#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 代码审查助手自检脚本。

用法：
    python test_api.py

说明：
    1. 检查首页、/docs、/health、/api/config 是否可访问。
    2. 检查访问密码、限流相关的基础行为。
    3. 会真实调用一次 /api/review，产生少量 DeepSeek token 消耗。
    4. 会检查审查历史、反馈和统计接口。
    5. 加 --with-stream 时额外调用一次 /api/review/stream，验证 SSE。
    6. 其余审查用例只验证参数校验，不会调用模型。
"""

import argparse
import json
import os
import sys

import requests

# 默认测试本机；也可以用 REVIEW_BASE_URL 指向部署后的地址
BASE_URL = os.getenv("REVIEW_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# 正常审查用例的超时时间（秒）
MODEL_TIMEOUT = 180

# 一段带 SQL 注入风险的示例代码，方便观察审查结果
SAMPLE_CODE = """def get_user(username):
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    return db.execute(query)"""

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条检查结果。"""
    global passed, failed
    if condition:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}")
        if detail:
            print(f"       {detail}")


def json_body(response: requests.Response) -> dict:
    """尽量把响应解析成 dict，解析失败时返回空 dict。"""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def post_review(
    session: requests.Session,
    payload: dict,
    timeout: int = 30,
) -> requests.Response:
    """调用审查接口。"""
    return session.post(
        f"{BASE_URL}/api/review",
        json=payload,
        timeout=timeout,
    )


def read_sse_response(response: requests.Response) -> tuple[str, bool]:
    """读取 SSE 响应，返回 (文本, 是否收到 done)。"""
    text = ""
    done = False

    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue

        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict):
            continue

        if event.get("type") == "delta":
            text += event.get("content") or ""
        elif event.get("type") == "done":
            done = True
        elif event.get("type") == "error":
            raise RuntimeError(event.get("message") or "流式返回错误")

    return text, done


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="AI 代码审查助手自检")
    parser.add_argument(
        "--with-stream",
        action="store_true",
        help="额外测试 SSE 流式接口（会多调用一次模型）",
    )
    args = parser.parse_args()

    print("=" * 64)
    print("AI 代码审查助手 - 自检脚本")
    print(f"目标服务：{BASE_URL}")
    print("注意：正常审查用例会真实调用一次 DeepSeek，产生少量 token 消耗。")
    print("=" * 64)

    with requests.Session() as session:
        # ---------- 1. 服务是否存活 ----------
        try:
            docs_resp = session.get(f"{BASE_URL}/docs", timeout=10)
        except requests.RequestException as exc:
            print(f"[FAIL] 无法连接 {BASE_URL}")
            print("       请先在 D:\\FastAPI 下启动服务：")
            print("       uvicorn main:app --reload")
            print(f"       错误信息：{exc}")
            return 1

        check(
            "GET /docs 返回 200",
            docs_resp.status_code == 200,
            f"实际返回 {docs_resp.status_code}",
        )

        # ---------- 2. OpenAPI 中是否有审查接口 ----------
        openapi_resp = session.get(f"{BASE_URL}/openapi.json", timeout=10)
        openapi = json_body(openapi_resp)
        paths = openapi.get("paths", {}) if isinstance(openapi, dict) else {}

        check(
            "GET /openapi.json 返回 200",
            openapi_resp.status_code == 200,
            f"实际返回 {openapi_resp.status_code}",
        )
        check(
            "OpenAPI 中存在 /api/review",
            "/api/review" in paths,
            f"实际 paths：{list(paths)[:5]}",
        )

        # ---------- 3. 首页 ----------
        home_resp = session.get(f"{BASE_URL}/", timeout=10)
        check(
            "GET / 返回 200",
            home_resp.status_code == 200,
            f"实际返回 {home_resp.status_code}",
        )
        check(
            "首页包含标题",
            "AI 代码审查助手" in home_resp.text,
            "首页没有找到预期标题",
        )

        # ---------- 4. 健康检查 ----------
        health_resp = session.get(f"{BASE_URL}/health", timeout=10)
        health_data = json_body(health_resp)
        check(
            "GET /health 返回 200",
            health_resp.status_code == 200,
            f"实际返回 {health_resp.status_code}",
        )
        check(
            "健康检查 status 为 ok",
            health_data.get("status") == "ok",
            f"实际返回：{health_resp.text[:300]}",
        )

        # ---------- 5. 前端配置 ----------
        config_resp = session.get(f"{BASE_URL}/api/config", timeout=10)
        config_data = json_body(config_resp)
        max_chars = int(config_data.get("max_code_chars") or 20000)

        check(
            "GET /api/config 返回 200",
            config_resp.status_code == 200,
            f"实际返回 {config_resp.status_code}",
        )
        check(
            "max_code_chars 大于 0",
            max_chars > 0,
            f"实际返回：{config_resp.text[:300]}",
        )
        check(
            "config 包含 auth_required",
            isinstance(config_data.get("auth_required"), bool),
            f"实际返回：{config_resp.text[:300]}",
        )

        # ---------- 6. 登录状态与访问控制 ----------
        me_resp = session.get(f"{BASE_URL}/api/me", timeout=10)
        me_data = json_body(me_resp)
        auth_required = bool(me_data.get("auth_required"))

        check(
            "GET /api/me 返回 200",
            me_resp.status_code == 200,
            f"实际返回 {me_resp.status_code}",
        )

        can_review = True
        if auth_required:
            anonymous_resp = post_review(
                session,
                {"code_snippet": "print(1)"},
                timeout=10,
            )
            check(
                "未登录调用返回 401",
                anonymous_resp.status_code == 401,
                f"实际返回 {anonymous_resp.status_code}：{anonymous_resp.text[:300]}",
            )

            password = os.getenv("ACCESS_PASSWORD", "")
            if not password:
                check(
                    "测试脚本已配置 ACCESS_PASSWORD",
                    False,
                    "服务启用了访问密码，请先设置同名环境变量再运行测试",
                )
                can_review = False
            else:
                login_resp = session.post(
                    f"{BASE_URL}/api/login",
                    json={"password": password},
                    timeout=10,
                )
                check(
                    "访问密码登录成功",
                    login_resp.status_code == 200,
                    f"实际返回 {login_resp.status_code}：{login_resp.text[:300]}",
                )
                can_review = login_resp.status_code == 200
        else:
            check("未启用访问密码（本机开发模式）", True)

        if can_review:
            # ---------- 7. 正常审查（会调用模型） ----------
            valid_payload = {
                "code_snippet": SAMPLE_CODE,
                "language": "python",
                "mode": "quick",
            }
            valid_resp = post_review(session, valid_payload, timeout=MODEL_TIMEOUT)
            valid_data = json_body(valid_resp)
            review = valid_data.get("review_result", "")

            check(
                "正常审查返回 200",
                valid_resp.status_code == 200,
                f"实际返回 {valid_resp.status_code}：{valid_resp.text[:300]}",
            )
            check(
                "status 为 success",
                valid_data.get("status") == "success",
                f"实际返回：{valid_resp.text[:300]}",
            )
            check(
                "mode 为 quick",
                valid_data.get("mode") == "quick",
                f"实际返回：{valid_resp.text[:300]}",
            )
            check(
                "review_result 非空",
                isinstance(review, str) and len(review.strip()) >= 20,
                f"实际长度：{len(review) if isinstance(review, str) else 'N/A'}",
            )

            review_id = valid_data.get("review_id")
            check(
                "返回 review_id",
                bool(review_id),
                f"实际返回：{valid_resp.text[:300]}",
            )

            if review_id:
                list_resp = session.get(
                    f"{BASE_URL}/api/reviews",
                    timeout=10,
                )
                check(
                    "GET /api/reviews 返回 200",
                    list_resp.status_code == 200,
                    f"实际返回 {list_resp.status_code}：{list_resp.text[:300]}",
                )

                detail_resp = session.get(
                    f"{BASE_URL}/api/reviews/{review_id}",
                    timeout=10,
                )
                check(
                    "GET /api/reviews/{id} 返回 200",
                    detail_resp.status_code == 200,
                    f"实际返回 {detail_resp.status_code}",
                )

                feedback_resp = session.post(
                    f"{BASE_URL}/api/reviews/{review_id}/feedback",
                    json={"verdict": "helpful"},
                    timeout=10,
                )
                check(
                    "历史反馈提交成功",
                    feedback_resp.status_code == 200,
                    f"实际返回 {feedback_resp.status_code}：{feedback_resp.text[:300]}",
                )

                insights_resp = session.get(
                    f"{BASE_URL}/api/insights",
                    timeout=10,
                )
                check(
                    "GET /api/insights 返回 200",
                    insights_resp.status_code == 200,
                    f"实际返回 {insights_resp.status_code}",
                )

            if isinstance(review, str) and review.strip():
                print("\n--- 审查结果预览（前 800 字）---")
                print(review[:800])
                if len(review) > 800:
                    print("...（已截断）")
                print("--- 预览结束 ---\n")

            # ---------- 8. 可选：SSE 流式接口 ----------
            if args.with_stream:
                with session.post(
                    f"{BASE_URL}/api/review/stream",
                    json=valid_payload,
                    stream=True,
                    timeout=MODEL_TIMEOUT,
                ) as stream_resp:
                    stream_detail = ""
                    if stream_resp.status_code != 200:
                        stream_detail = (
                            f"实际返回 {stream_resp.status_code}："
                            f"{stream_resp.text[:300]}"
                        )
                    check(
                        "流式接口返回 200",
                        stream_resp.status_code == 200,
                        stream_detail,
                    )

                    if stream_resp.status_code == 200:
                        try:
                            stream_text, stream_done = read_sse_response(stream_resp)
                            check(
                                "流式输出非空",
                                len(stream_text.strip()) >= 20,
                                f"实际长度：{len(stream_text)}",
                            )
                            check("流式输出收到 done", stream_done)
                            if stream_text:
                                print("\n--- 流式输出预览（前 200 字）---")
                                print(stream_text[:200])
                                print("--- 预览结束 ---\n")
                        except Exception as exc:
                            check("流式输出解析成功", False, str(exc))

            # ---------- 9. 超长代码：应 413 ----------
            too_long_resp = post_review(
                session,
                {"code_snippet": "x" * (max_chars + 1)},
            )
            check(
                "超长代码返回 413",
                too_long_resp.status_code == 413,
                f"实际返回 {too_long_resp.status_code}：{too_long_resp.text[:300]}",
            )

            # ---------- 10. 不支持的语言：应 400 ----------
            bad_lang_resp = post_review(
                session,
                {"code_snippet": "print(1)", "language": "not-a-language"},
            )
            check(
                "不支持的语言返回 400",
                bad_lang_resp.status_code == 400,
                f"实际返回 {bad_lang_resp.status_code}：{bad_lang_resp.text[:300]}",
            )
            check(
                "400 提示不支持的代码语言",
                "不支持的代码语言" in bad_lang_resp.text,
                f"实际返回：{bad_lang_resp.text[:300]}",
            )

            # ---------- 11. 缺少必填字段：应 422 ----------
            missing_resp = post_review(session, {})
            missing_data = json_body(missing_resp)
            detail = missing_data.get("detail")
            has_code_snippet_error = isinstance(detail, list) and any(
                isinstance(item, dict) and "code_snippet" in item.get("loc", [])
                for item in detail
            )

            check(
                "缺少 code_snippet 返回 422",
                missing_resp.status_code == 422,
                f"实际返回 {missing_resp.status_code}：{missing_resp.text[:300]}",
            )
            check(
                "422 指出 code_snippet 缺失",
                has_code_snippet_error,
                f"实际返回：{missing_resp.text[:300]}",
            )

            # ---------- 12. 空字符串：应 422 ----------
            empty_resp = post_review(session, {"code_snippet": ""})
            check(
                "code_snippet 为空字符串返回 422",
                empty_resp.status_code == 422,
                f"实际返回 {empty_resp.status_code}：{empty_resp.text[:300]}",
            )

            # ---------- 13. 纯空格：应 400（业务校验） ----------
            spaces_resp = post_review(session, {"code_snippet": "   "})
            check(
                "code_snippet 为纯空格返回 400",
                spaces_resp.status_code == 400,
                f"实际返回 {spaces_resp.status_code}：{spaces_resp.text[:300]}",
            )
            check(
                "400 提示代码不能为空",
                "代码不能为空" in spaces_resp.text,
                f"实际返回：{spaces_resp.text[:300]}",
            )

            # ---------- 14. 类型错误：应 422 ----------
            type_resp = post_review(session, {"code_snippet": 123})
            check(
                "code_snippet 为数字返回 422",
                type_resp.status_code == 422,
                f"实际返回 {type_resp.status_code}：{type_resp.text[:300]}",
            )
        else:
            print("\n跳过审查用例：请先配置访问密码，再重新运行。")

    # ---------- 汇总 ----------
    print("=" * 64)
    print(f"自检结果：{passed} 项通过，{failed} 项失败")
    print("=" * 64)

    if failed:
        print("排查建议：")
        print("1. 确认 uvicorn 已启动，并且加载的是最新的 main.py")
        print("2. 确认已设置 DEEPSEEK_API_KEY，并且重启过服务")
        print("3. 如果启用了访问密码，确认测试脚本的 ACCESS_PASSWORD 与服务一致")
        print("4. 查看 uvicorn 终端日志中 DeepSeek 的返回信息")
        return 1

    print("全部通过：网页、访问控制、参数校验、DeepSeek 调用均正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
