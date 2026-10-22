"""Zip 文件夹分析。

设计原则：
1. 只解压到内存/临时对象，不落盘保存用户代码。
2. 默认跳过 .env、密钥、二进制、依赖目录。
3. 只把 Top N 个高风险文件发给模型，不整仓库塞上下文。
4. 报告里明确说明“抽样审查”，不是逐行全仓库审查。
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from pathlib import PurePosixPath

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
    ".pytest_cache",
    "coverage",
    "target",
    "bin",
    "obj",
}

IGNORED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".rar",
    ".7z",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".map",
    ".lock",
}

SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_dsa",
    "id_ed25519",
    "secrets.yml",
    "secrets.yaml",
    "credentials.json",
    "service-account.json",
}

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".php": "php",
    ".rb": "ruby",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".sql": "sql",
    ".sh": "bash",
    ".html": "html",
    ".css": "css",
    ".vue": "vue",
}

SENSITIVE_PATTERNS = [
    re.compile(r"-----BEGIN (RSA|OPENSSH|EC|PGP) PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(
        r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token)"
        r"\s*[:=]\s*[\"'][^\"']{8,}[\"']"
    ),
]

RISK_PATTERNS = [
    (re.compile(r"\b(eval|exec)\s*\("), 4),
    (re.compile(r"\b(os\.system|subprocess|popen|shell\s*=\s*True)\b"), 4),
    (re.compile(r"\b(pickle\.loads|yaml\.load|marshal\.loads)\b"), 3),
    (re.compile(r"(?i)\b(select|insert|update|delete)\b.*(\+|\{|\%)"), 2),
    (re.compile(r"(?i)(password|secret|token|api[_-]?key)\s*="), 1),
    (re.compile(r"\b(innerHTML|dangerouslySetInnerHTML)\b"), 3),
    (re.compile(r"\b(open|readFile|writeFile)\s*\("), 1),
    (re.compile(r"(?i)\b(auth|jwt|session|crypto)\b"), 1),
]

ENTRYPOINT_HINTS = (
    "main",
    "app",
    "route",
    "controller",
    "service",
    "auth",
    "payment",
    "admin",
    "api",
)

MAX_FILES_IN_ZIP = 500
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_TOTAL_CHARS = 200_000


def _language_for(path: PurePosixPath) -> str | None:
    return LANGUAGE_MAP.get(path.suffix.lower())


def _is_ignored(path: PurePosixPath) -> bool:
    if any(part in IGNORED_DIRS for part in path.parts):
        return True
    name = path.name.lower()
    if name.endswith(".min.js") or name.endswith(".min.css"):
        return True
    return path.suffix.lower() in IGNORED_SUFFIXES


def _is_sensitive_name(path: PurePosixPath) -> bool:
    name = path.name.lower()
    if name in SENSITIVE_NAMES:
        return True
    return name.startswith(".env") and name != ".env.example"


def _is_sensitive(path: PurePosixPath, content: str) -> bool:
    if _is_sensitive_name(path):
        return True
    return any(pattern.search(content) for pattern in SENSITIVE_PATTERNS)


def _risk_score(path: PurePosixPath, content: str, lines: int) -> int:
    score = min(lines / 100, 5)
    for pattern, weight in RISK_PATTERNS:
        matches = len(pattern.findall(content))
        if matches:
            score += min(matches, 3) * weight
    path_text = str(path).lower()
    if any(hint in path_text for hint in ENTRYPOINT_HINTS):
        score += 3
    if "test" in path_text or "spec" in path_text:
        score -= 2
    return int(max(score, 0))


def _extract_files(
    zip_bytes: bytes,
    *,
    max_file_chars: int,
) -> tuple[list[dict], list[str], list[str], int]:
    code_files: list[dict] = []
    sensitive: list[str] = []
    skipped: list[str] = []
    total_chars = 0

    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的 zip 文件") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES_IN_ZIP:
            raise ValueError(f"压缩包文件太多，最多 {MAX_FILES_IN_ZIP} 个")

        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("压缩包解压后过大")

        for info in infos:
            if info.is_dir():
                continue

            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                skipped.append(name)
                continue

            path = PurePosixPath(name)
            if ".." in path.parts:
                skipped.append(name)
                continue

            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                skipped.append(name)
                continue

            if _is_ignored(path):
                continue

            if _is_sensitive_name(path):
                sensitive.append(name)
                continue

            language = _language_for(path)
            if language is None:
                continue

            if info.file_size > max_file_chars * 6:
                skipped.append(name)
                continue

            with archive.open(info) as file:
                raw = file.read(max_file_chars * 6 + 1)

            if len(raw) > max_file_chars * 6:
                skipped.append(name)
                continue
            if b"\x00" in raw[:1000]:
                skipped.append(name)
                continue

            content = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
            if len(content) > max_file_chars:
                skipped.append(name)
                continue
            if _is_sensitive(path, content):
                sensitive.append(name)
                continue
            if total_chars + len(content) > MAX_TOTAL_CHARS:
                skipped.append(name)
                continue

            lines = content.count("\n") + 1
            code_files.append(
                {
                    "path": str(path),
                    "language": language,
                    "content": content,
                    "lines": lines,
                    "score": _risk_score(path, content, lines),
                }
            )
            total_chars += len(content)

    return code_files, sensitive, skipped, total_chars


def _project_map(code_files: list[dict], limit: int = 200) -> str:
    paths = sorted(item["path"] for item in code_files)
    visible = paths[:limit]
    lines = [f"- {path}" for path in visible]
    if len(paths) > limit:
        lines.append(f"- ...（还有 {len(paths) - limit} 个文件）")
    return "\n".join(lines)


def _imports_for(content: str, limit: int = 15) -> str:
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(r"^(import|from|require|use|#include)\b", stripped):
            lines.append(stripped)
            if len(lines) >= limit:
                break
    return "\n".join(lines)


def _request_review(
    prompt: str,
    *,
    api_key: str,
    api_url: str,
    model: str,
    max_tokens: int,
    timeout: int,
) -> tuple[str, dict]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个严谨的代码审查工程师。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=(10, timeout),
        )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty review content")
        return text, usage
    except Exception as exc:
        return f"> 审查失败：{type(exc).__name__}: {exc}", {}


def _review_file(
    file: dict,
    *,
    project_map: str,
    api_key: str,
    api_url: str,
    model: str,
    max_tokens: int,
    timeout: int,
) -> tuple[str, dict]:
    imports = _imports_for(file["content"])
    prompt = (
        f"你是资深{file['language']}开发工程师。下面是项目中的部分文件树：\n"
        f"{project_map}\n\n"
        f"当前审查文件：{file['path']}\n"
        f"相关 import：\n{imports or '（无）'}\n\n"
        f"```{file['language']}\n{file['content']}\n```\n\n"
        "要求：\n"
        "1. 只报告你能从当前文件和文件树确认的问题。\n"
        "2. 如果依赖其他文件但未提供，标记“需要确认”，不要直接判定未定义。\n"
        "3. 最多报告 3 条高置信度问题，按严重程度排序。\n"
        "4. 每条给：位置、原因、修复建议。\n"
        "5. 不要复述代码，不要写客套话。"
    )
    return _request_review(
        prompt,
        api_key=api_key,
        api_url=api_url,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def _review_bundle(
    bundle,
    bundle_files: list[dict],
    *,
    project_map: str,
    api_key: str,
    api_url: str,
    model: str,
    max_tokens: int,
    timeout: int,
) -> tuple[str, dict]:
    by_path = {item["path"]: item for item in bundle_files}
    ordered = []
    if bundle.entry in by_path:
        ordered.append(by_path[bundle.entry])
    for path in bundle.files:
        item = by_path.get(path)
        if item is not None and item not in ordered:
            ordered.append(item)

    chunks = []
    total_chars = 0
    for item in ordered[:6]:
        if total_chars + len(item["content"]) > 60_000:
            break
        chunks.append(
            f"--- {item['path']} ---\n"
            f"```{item['language']}\n{item['content']}\n```"
        )
        total_chars += len(item["content"])

    relations = "\n".join(
        f"- {relation['from']} -> {relation['to']} ({relation['type']})"
        for relation in bundle.relations[:20]
    )
    experiment_note = (
        "\n注意：这是框架适配器的实验性 bundle，跨文件关系可能不完整。"
        if bundle.experimental
        else ""
    )
    prompt = (
        f"你是资深开发工程师。项目框架：{bundle.framework}。\n"
        f"审查包：{bundle.id}\n入口文件：{bundle.entry}{experiment_note}\n\n"
        f"项目文件树：\n{project_map}\n\n"
        f"审查包内文件：\n\n" + "\n\n".join(chunks) + "\n\n"
        f"文件关系：\n{relations or '（无）'}\n\n"
        "要求：\n"
        "1. 跨文件审查，不要把包内其他文件定义的符号判定为未定义。\n"
        "2. 如果依赖不在包内，标记“需要确认”，不要直接判定未定义。\n"
        "3. 最多报告 5 条高置信度问题，按严重程度排序。\n"
        "4. 每条给：文件、位置、原因、修复建议。\n"
        "5. 不要复述代码，不要写客套话。"
    )
    return _request_review(
        prompt,
        api_key=api_key,
        api_url=api_url,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def analyze_folder(
    zip_bytes: bytes,
    *,
    api_key: str,
    api_url: str,
    model: str,
    max_files: int,
    max_file_chars: int,
    max_output_tokens: int,
    timeout: int,
) -> dict:
    code_files, sensitive, skipped, total_chars = _extract_files(
        zip_bytes,
        max_file_chars=max_file_chars,
    )

    if not code_files:
        raise ValueError("没有找到可审查的代码文件")

    project_map = _project_map(code_files)
    by_path = {item["path"]: item for item in code_files}
    bundles = build_bundles(code_files)

    sections = []
    total_usage: dict[str, int] = {}
    selected_files: list[dict] = []
    selected_bundles = []
    frameworks: list[str] = []

    if bundles:
        max_bundles = max(1, min(max_files, 5))
        seen_paths: set[str] = set()

        for bundle in bundles[:max_bundles]:
            bundle_files = [
                by_path[path] for path in bundle.files if path in by_path
            ]
            if not bundle_files:
                continue

            text, usage = _review_bundle(
                bundle,
                bundle_files,
                project_map=project_map,
                api_key=api_key,
                api_url=api_url,
                model=model,
                max_tokens=max_output_tokens,
                timeout=timeout,
            )
            for key, value in usage.items():
                if isinstance(value, int):
                    total_usage[key] = total_usage.get(key, 0) + value

            file_list = "\n".join(f"- {path}" for path in bundle.files)
            experiment = " · 实验性" if bundle.experimental else ""
            sections.append(
                f"### {bundle.id}  \n"
                f"框架：{bundle.framework}{experiment} · "
                f"入口：{bundle.entry} · 风险分：{bundle.score}  \n"
                f"包含文件：\n{file_list}\n\n"
                f"{text.strip()}"
            )
            selected_bundles.append(bundle)
            frameworks.append(bundle.framework)

            for path in bundle.files:
                item = by_path.get(path)
                if item is not None and path not in seen_paths:
                    seen_paths.add(path)
                    selected_files.append(item)

    if not selected_bundles:
        ranked = sorted(
            code_files,
            key=lambda item: (item["score"], item["lines"]),
            reverse=True,
        )
        selected_files = ranked[:max_files]
        for file in selected_files:
            text, usage = _review_file(
                file,
                project_map=project_map,
                api_key=api_key,
                api_url=api_url,
                model=model,
                max_tokens=max_output_tokens,
                timeout=timeout,
            )
            for key, value in usage.items():
                if isinstance(value, int):
                    total_usage[key] = total_usage.get(key, 0) + value

            sections.append(
                f"### {file['path']}  \n"
                f"风险分：{file['score']} · {file['language']} · "
                f"{file['lines']} 行\n\n"
                f"{text.strip()}"
            )

    languages: dict[str, int] = {}
    for file in code_files:
        languages[file["language"]] = languages.get(file["language"], 0) + 1
    language_summary = "、".join(
        f"{name} {count}" for name, count in sorted(
            languages.items(), key=lambda item: item[1], reverse=True
        )
    )

    if selected_bundles:
        analysis_line = (
            f"- 本次抽样分析：{len(selected_bundles)} 个审查包"
            f"（{len(selected_files)} 个文件）"
        )
        framework_line = f"- 识别框架：{'、'.join(sorted(set(frameworks)))}"
        section_title = "## 审查包"
    else:
        analysis_line = f"- 本次抽样分析：{len(selected_files)} 个高风险文件"
        framework_line = "- 识别框架：未识别到专用适配器"
        section_title = "## 重点文件"

    report_lines = [
        "# 文件夹分析报告",
        "",
        "## 概览",
        "",
        f"- 识别代码文件：{len(code_files)} 个",
        f"- 代码字符数：{total_chars}",
        f"- 语言分布：{language_summary}",
        framework_line,
        analysis_line,
        f"- 跳过敏感文件：{len(sensitive)} 个",
        f"- 跳过其他文件：{len(skipped)} 个",
        "",
        section_title,
        "",
        "\n\n".join(sections),
        "",
        "## 说明",
        "",
        "这是抽样审查，不是全仓库逐行审查。",
        "审查包按 import/调用链生成；每个包最多 6 个文件进入模型。",
        "包外依赖仍可能不完整，无法确认的符号会标记“需要确认”。",
    ]

    if sensitive:
        report_lines.extend(
            [
                "",
                "## 已跳过的敏感文件",
                "",
                "\n".join(f"- {name}" for name in sorted(set(sensitive))[:20]),
                "",
                "这些文件没有上传给模型，也没有写入报告。",
            ]
        )

    return {
        "status": "success",
        "report": "\n".join(report_lines),
        "summary": {
            "code_files": len(code_files),
            "selected_files": len(selected_files),
            "bundles": len(selected_bundles),
            "sensitive_files": len(sensitive),
            "skipped_files": len(skipped),
            "total_chars": total_chars,
            "languages": languages,
            "frameworks": sorted(set(frameworks)),
        },
        "selected_files": [
            {
                "path": item["path"],
                "language": item["language"],
                "score": item["score"],
                "lines": item["lines"],
            }
            for item in selected_files
        ],
        "bundles": [
            {
                "id": bundle.id,
                "framework": bundle.framework,
                "entry": bundle.entry,
                "files": bundle.files,
                "score": bundle.score,
                "experimental": bundle.experimental,
            }
            for bundle in selected_bundles
        ],
        "skipped_sensitive": sorted(set(sensitive))[:50],
        "usage": total_usage,
    }
