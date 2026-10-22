#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 代码审查评测脚本。

用法：
    python eval/run_eval.py
    python eval/run_eval.py --limit 3
    python eval/run_eval.py --base-url https://your-domain.com --password your-password

说明：
    这是基于关键词命中的工程冒烟评测，不等同于严格的准确率评测。
    它适合在修改 Prompt、模型或审查逻辑后，快速发现明显回归。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from .cases import CASES
except ImportError:
    from cases import CASES

BASE_DIR = Path(__file__).resolve().parent


def json_body(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def login(session: requests.Session, base_url: str, password: str) -> bool:
    try:
        me_response = session.get(f"{base_url}/api/me", timeout=10)
    except requests.RequestException as exc:
        print(f"无法连接 {base_url}：{exc}")
        return False

    me = json_body(me_response)
    if not me.get("auth_required"):
        return True

    if not password:
        print("服务启用了访问密码，请通过 --password 或 ACCESS_PASSWORD 提供。")
        return False

    response = session.post(
        f"{base_url}/api/login",
        json={"password": password},
        timeout=10,
    )
    if response.status_code != 200:
        print(f"登录失败：HTTP {response.status_code} {response.text[:200]}")
        return False
    return True


def run_case(
    session: requests.Session,
    base_url: str,
    case: dict,
    timeout: int,
    mode: str = "deep",
) -> dict:
    payload = {
        "code_snippet": case["code"],
        "language": case["language"],
        "mode": mode,
    }

    started = time.perf_counter()
    response = session.post(
        f"{base_url}/api/review",
        json=payload,
        timeout=timeout,
    )
    latency_ms = round((time.perf_counter() - started) * 1000)

    if response.status_code == 429:
        print("  触发限流，等待 60 秒后重试……")
        time.sleep(60)
        started = time.perf_counter()
        response = session.post(
            f"{base_url}/api/review",
            json=payload,
            timeout=timeout,
        )
        latency_ms = round((time.perf_counter() - started) * 1000)

    body = json_body(response)
    if response.status_code != 200:
        return {
            "id": case["id"],
            "title": case["title"],
            "passed": False,
            "matched": [],
            "latency_ms": latency_ms,
            "status_code": response.status_code,
            "error": body.get("detail") or response.text[:300],
        }

    review = body.get("review_result") or ""
    review_lower = review.lower()
    matched = [
        keyword
        for keyword in case["expected"]
        if keyword.lower() in review_lower
    ]

    return {
        "id": case["id"],
        "title": case["title"],
        "passed": bool(matched),
        "matched": matched,
        "latency_ms": latency_ms,
        "status_code": response.status_code,
        "usage": body.get("usage") or {},
        "review_preview": review[:400],
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="AI 代码审查关键词命中评测")
    parser.add_argument(
        "--base-url",
        default=os.getenv("REVIEW_BASE_URL", "http://127.0.0.1:8000"),
        help="服务地址，默认 http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("ACCESS_PASSWORD", ""),
        help="服务访问密码；服务未启用密码时不需要",
    )
    parser.add_argument("--timeout", type=int, default=180, help="单次请求超时秒数")
    parser.add_argument(
        "--mode",
        choices=["quick", "deep"],
        default="deep",
        help="审查模式：deep 保留完整报告，quick 只列 Top 3",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="用例之间的间隔秒数")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个用例，0 表示全部")
    parser.add_argument(
        "--output",
        default="results.json",
        help="结果 JSON 路径，相对路径基于 eval/ 目录",
    )
    parser.add_argument(
        "--min-rate",
        type=float,
        default=0.0,
        help="最低检出率，低于该值时返回非 0；默认 0",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    cases = CASES[: args.limit] if args.limit > 0 else CASES

    print("=" * 72)
    print("AI 代码审查评测")
    print(f"服务：{base_url}")
    print(f"用例：{len(cases)}")
    print("=" * 72)

    session = requests.Session()
    if not login(session, base_url, args.password):
        return 1

    try:
        config = json_body(session.get(f"{base_url}/api/config", timeout=10))
    except requests.RequestException:
        config = {}

    results = []
    for index, case in enumerate(cases, 1):
        print(
            f"[{index}/{len(cases)}] {case['id']} - {case['title']} ...",
            end=" ",
            flush=True,
        )
        try:
            result = run_case(session, base_url, case, args.timeout, args.mode)
        except requests.Timeout:
            result = {
                "id": case["id"],
                "title": case["title"],
                "passed": False,
                "matched": [],
                "latency_ms": 0,
                "status_code": None,
                "error": "请求超时",
            }
        except requests.RequestException as exc:
            result = {
                "id": case["id"],
                "title": case["title"],
                "passed": False,
                "matched": [],
                "latency_ms": 0,
                "status_code": None,
                "error": f"请求失败：{exc}",
            }

        results.append(result)
        print(f"{'PASS' if result['passed'] else 'FAIL'} ({result['latency_ms']} ms)")
        if result.get("matched"):
            print(f"    命中：{', '.join(result['matched'])}")
        elif result.get("error"):
            print(f"    错误：{result['error']}")

        if args.delay > 0 and index < len(cases):
            time.sleep(args.delay)

    total = len(results)
    passed = sum(1 for result in results if result["passed"])
    rate = passed / total if total else 0.0
    latencies = [result["latency_ms"] for result in results if result["latency_ms"]]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    total_tokens = sum(
        (result.get("usage") or {}).get("total_tokens") or 0
        for result in results
        if isinstance(result.get("usage"), dict)
    )

    print("=" * 72)
    print(f"检出率：{passed}/{total} ({rate:.0%})")
    print(f"平均延迟：{avg_latency:.0f} ms")
    print(f"总 token：{total_tokens}")
    print("=" * 72)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = BASE_DIR / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "model": config.get("model"),
        "summary": {
            "total": total,
            "passed": passed,
            "detection_rate": round(rate, 4),
            "avg_latency_ms": round(avg_latency, 1),
            "total_tokens": total_tokens,
        },
        "results": results,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"结果已写入：{output_path}")

    if rate < args.min_rate:
        print(f"检出率低于阈值 {args.min_rate:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
