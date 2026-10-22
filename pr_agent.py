"""本地 PR 审查 Agent。

目标：先跑通“diff -> 上下文 -> 测试 -> 审查意见”的闭环，
再考虑接 GitHub App / Webhook。

用法：
    python pr_agent.py --repo . --diff changes.patch --run-tests
    python pr_agent.py --repo . --base HEAD~1 --run-tests --dry-run

说明：
- --dry-run 不调用模型，只验证 diff、上下文和测试执行。
- 默认测试命令：python -m pytest -q
- 也可以改成：--test-command "python -m unittest discover -q"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

from review_bundles import build_bundles

IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".idea",
    ".vscode",
}

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
}

SENSITIVE_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.yml",
    "secrets.yaml",
}


def parse_unified_diff(text: str) -> list[dict]:
    files: list[dict] = []
    current: dict | None = None
    new_line = 0

    for raw in text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path == "/dev/null":
                current = None
            else:
                current = {"path": path, "added": []}
                files.append(current)
            continue

        if raw.startswith("@@") and current is not None:
            match = re.search(r"\+(\d+)", raw)
            if match:
                new_line = int(match.group(1))
            continue

        if current is None:
            continue

        if raw.startswith("+") and not raw.startswith("+++"):
            current["added"].append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith(" "):
            new_line += 1

    return [item for item in files if item["added"]]


def collect_repo_files(repo: Path, max_file_chars: int = 20_000) -> list[dict]:
    files: list[dict] = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(repo)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.name.lower() in SENSITIVE_NAMES:
            continue
        if path.name.lower().startswith(".env"):
            continue
        language = LANGUAGE_MAP.get(path.suffix.lower())
        if language is None:
            continue
        try:
            content = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if "\x00" in content[:1000] or len(content) > max_file_chars:
            continue
        files.append(
            {
                "path": relative.as_posix(),
                "language": language,
                "content": content,
                "lines": content.count("\n") + 1,
                "score": 0,
            }
        )
    return files


def select_context(
    files: list[dict],
    changed_paths: set[str],
    *,
    max_bundles: int = 3,
) -> tuple[str, list[dict], list[dict]]:
    by_path = {item["path"]: item for item in files}
    bundles = build_bundles(files)
    selected = [
        bundle
        for bundle in bundles
        if set(bundle.files) & changed_paths
    ][:max_bundles]

    chunks: list[str] = []
    included_files: list[str] = []
    total = 0

    if selected:
        for bundle in selected:
            for path in bundle.files:
                item = by_path.get(path)
                if item is None or path in included_files:
                    continue
                if total + len(item["content"]) > 60_000:
                    break
                chunks.append(
                    f"--- {path} ---\n"
                    f"```{item['language']}\n{item['content']}\n```"
                )
                included_files.append(path)
                total += len(item["content"])
    else:
        for path in sorted(changed_paths):
            item = by_path.get(path)
            if item is None:
                continue
            chunks.append(
                f"--- {path} ---\n"
                f"```{item['language']}\n{item['content']}\n```"
            )
            included_files.append(path)

    return "\n\n".join(chunks), selected, [
        {"path": path} for path in included_files
    ]


def run_command(repo: Path, command: str, timeout: int) -> dict | None:
    if not command.strip():
        return None
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "") + (result.stderr or "")
        return {
            "command": command,
            "returncode": result.returncode,
            "output": output[-8000:],
        }
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "returncode": None,
            "output": f"测试超时（>{timeout}s）",
        }
    except Exception as exc:
        return {
            "command": command,
            "returncode": None,
            "output": f"测试执行失败：{type(exc).__name__}: {exc}",
        }


def build_prompt(
    diff_text: str,
    context_text: str,
    changed: list[dict],
    test_result: dict | None,
) -> str:
    changed_text = "\n".join(
        f"- {item['path']}（新增行：{len(item['added'])}）"
        for item in changed
    )
    test_text = "未运行测试。"
    if test_result is not None:
        test_text = (
            f"命令：{test_result['command']}\n"
            f"返回码：{test_result['returncode']}\n"
            f"输出：\n{test_result['output']}"
        )

    return (
        "你是一个严谨的 PR 审查 Agent。请只审查本次 diff 引入的问题。\n\n"
        f"变更文件：\n{changed_text}\n\n"
        f"Diff：\n```diff\n{diff_text[:20000]}\n```\n\n"
        f"相关上下文：\n{context_text}\n\n"
        f"测试结果：\n{test_text}\n\n"
        "要求：\n"
        "1. 只评论新增/修改行，不要评论未改动代码。\n"
        "2. 如果依赖文件未提供，标记“需要确认”，不要直接判定未定义。\n"
        "3. 优先指出会导致测试失败、运行时错误、安全问题或契约破坏的问题。\n"
        "4. 输出 JSON，不要输出 Markdown 代码块：\n"
        "{\n"
        '  "summary": "一句话总结",\n'
        '  "comments": [\n'
        "    {\n"
        '      "path": "文件路径",\n'
        '      "line": 12,\n'
        '      "severity": "critical|major|minor",\n'
        '      "body": "问题 + 修复建议"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "5. 最多 8 条评论。"
    )


def call_model(prompt: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY")
    api_url = os.getenv(
        "DEEPSEEK_API_URL",
        "https://api.deepseek.com/chat/completions",
    )
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    response = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个严谨的 PR 审查工程师。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 2000,
        },
        timeout=(10, 120),
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def parse_model_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def render_report(
    changed: list[dict],
    context_bundles: list[dict],
    test_result: dict | None,
    review_text: str,
) -> str:
    lines = [
        "# PR Review Agent",
        "",
        "## 变更文件",
        "",
        "\n".join(
            f"- `{item['path']}`：新增 {len(item['added'])} 行"
            for item in changed
        ),
        "",
        "## 上下文 Bundle",
        "",
        "\n".join(
            f"- `{bundle.id}`：{', '.join(bundle.files)}"
            for bundle in context_bundles
        ) or "- 未识别到专用 Bundle，使用变更文件本身",
        "",
        "## 测试结果",
        "",
    ]

    if test_result is None:
        lines.append("未运行测试。")
    else:
        lines.extend(
            [
                f"- 命令：`{test_result['command']}`",
                f"- 返回码：`{test_result['returncode']}`",
                "",
                "```text",
                test_result["output"],
                "```",
            ]
        )

    lines.extend(["", "## 审查意见", ""])
    data = parse_model_json(review_text)
    if data:
        lines.append(data.get("summary", ""))
        lines.append("")
        for comment in data.get("comments", []):
            lines.append(
                f"### `{comment.get('path')}:{comment.get('line')}` "
                f"[{comment.get('severity', 'minor')}]"
            )
            lines.append("")
            lines.append(comment.get("body", ""))
            lines.append("")
    else:
        lines.append(review_text)

    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="本地 PR 审查 Agent")
    parser.add_argument("--repo", default=".", help="仓库根目录")
    parser.add_argument("--diff", help="unified diff 文件；不传则用 git diff")
    parser.add_argument("--base", default="HEAD~1", help="git diff 的 base")
    parser.add_argument("--run-tests", action="store_true", help="运行测试")
    parser.add_argument(
        "--test-command",
        default="python -m pytest -q",
        help="测试命令",
    )
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="不调用模型")
    parser.add_argument("--output", help="报告输出路径")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"仓库不存在：{repo}", file=sys.stderr)
        return 1

    if args.diff:
        diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    else:
        result = subprocess.run(
            ["git", "diff", "--unified=0", f"{args.base}...HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return 1
        diff_text = result.stdout

    changed = parse_unified_diff(diff_text)
    if not changed:
        print("diff 中没有新增行。")
        return 0

    files = collect_repo_files(repo)
    changed_paths = {item["path"] for item in changed}
    context_text, bundles, _ = select_context(files, changed_paths)

    test_result = None
    if args.run_tests:
        test_result = run_command(repo, args.test_command, args.test_timeout)

    if args.dry_run:
        review_text = "（dry-run，未调用模型）"
    else:
        prompt = build_prompt(diff_text, context_text, changed, test_result)
        try:
            review_text = call_model(prompt)
        except Exception as exc:
            print(f"调用模型失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    report = render_report(changed, bundles, test_result, review_text)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"报告已写入：{args.output}")
    else:
        print(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
