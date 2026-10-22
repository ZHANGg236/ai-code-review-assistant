"""框架适配器 + 审查包（Bundle）最小骨架。

目标：
1. 不再按单个文件审查，而是把调用链相关文件打成 Bundle。
2. 先支持 Python/pytest，再给 Spring Boot 留适配器接口。
3. 不依赖第三方解析库，Python 用 ast，Java 先用轻量正则。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass
class Bundle:
    id: str
    framework: str
    entry: str
    files: list[str]
    relations: list[dict] = field(default_factory=list)
    score: int = 0
    experimental: bool = False


class FrameworkAdapter:
    name = "base"

    def detect(self, files: list[dict]) -> bool:
        return False

    def build_bundles(self, files: list[dict]) -> list[Bundle]:
        return []


def _module_names(path: str) -> set[str]:
    if not path.endswith(".py"):
        return set()
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join(parts[index:]) for index in range(len(parts))}


def _parse_python(content: str) -> ast.AST | None:
    try:
        return ast.parse(content.lstrip("\ufeff"))
    except SyntaxError:
        return None


def _resolve_module(module: str, module_to_path: dict[str, str], by_path: dict[str, dict]) -> str | None:
    if module in module_to_path:
        return module_to_path[module]
    base = module.replace(".", "/")
    for suffix in (".py", "/__init__.py"):
        candidate = base + suffix
        if candidate in by_path:
            return candidate
    return None


def _conftest_candidates(path: str) -> list[str]:
    parts = list(PurePosixPath(path).parts[:-1])
    candidates = []
    for index in range(len(parts), -1, -1):
        prefix = "/".join(parts[:index])
        candidate = f"{prefix}/conftest.py" if prefix else "conftest.py"
        candidates.append(candidate)
    return candidates


def _fixtures(content: str) -> set[str]:
    names: set[str] = set()
    tree = _parse_python(content)
    if tree is None:
        return names

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Name) and target.id == "fixture":
                names.add(node.name)
            elif isinstance(target, ast.Attribute) and target.attr == "fixture":
                names.add(node.name)
    return names


def _test_arguments(content: str) -> set[str]:
    names: set[str] = set()
    tree = _parse_python(content)
    if tree is None:
        return names

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        for arg in node.args.args:
            names.add(arg.arg)
    return names


class PythonPytestAdapter(FrameworkAdapter):
    name = "python-pytest"

    def detect(self, files: list[dict]) -> bool:
        return any(item["path"].endswith(".py") for item in files)

    def build_bundles(self, files: list[dict]) -> list[Bundle]:
        by_path = {item["path"]: item for item in files}
        module_to_path: dict[str, str] = {}
        for path in by_path:
            for module in _module_names(path):
                module_to_path.setdefault(module, path)

        forward: dict[str, set[str]] = {path: set() for path in by_path}
        reverse: dict[str, set[str]] = {path: set() for path in by_path}

        for path, item in by_path.items():
            tree = _parse_python(item["content"])
            if tree is None:
                continue
            modules: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.level == 0:
                        modules.add(node.module)

            for module in modules:
                target = _resolve_module(module, module_to_path, by_path)
                if target and target != path:
                    forward[path].add(target)
                    reverse[target].add(path)

        conftest_fixtures: dict[str, set[str]] = {}
        for path, item in by_path.items():
            if PurePosixPath(path).name == "conftest.py":
                conftest_fixtures[path] = _fixtures(item["content"])

        def related_files(entry: str, depth: int = 2) -> set[str]:
            selected = {entry}
            frontier = {entry}
            for _ in range(depth):
                next_frontier: set[str] = set()
                for current in frontier:
                    for target in forward[current]:
                        if target not in selected:
                            selected.add(target)
                            next_frontier.add(target)
                frontier = next_frontier
            return selected

        def relations_for(selected: set[str]) -> list[dict]:
            relations = []
            for source in selected:
                for target in forward[source]:
                    if target in selected:
                        relations.append(
                            {"from": source, "to": target, "type": "import"}
                        )
            return relations

        bundles: list[Bundle] = []
        test_paths = {
            path
            for path in by_path
            if PurePosixPath(path).name.startswith("test_")
            or PurePosixPath(path).name.endswith("_test.py")
        }

        for path in sorted(test_paths):
            selected = related_files(path, depth=2)
            arguments = _test_arguments(by_path[path]["content"])
            for candidate in _conftest_candidates(path):
                if candidate in by_path:
                    selected.add(candidate)
                    if conftest_fixtures.get(candidate, set()) & arguments:
                        selected.add(candidate)

            score = sum(by_path[item].get("score", 0) for item in selected) + 5
            bundles.append(
                Bundle(
                    id=f"test:{PurePosixPath(path).stem}",
                    framework=self.name,
                    entry=path,
                    files=sorted(selected),
                    relations=relations_for(selected),
                    score=score,
                )
            )

        for path in sorted(by_path):
            if path in test_paths:
                continue
            selected = {path} | set(list(reverse[path])[:5])
            score = sum(by_path[item].get("score", 0) for item in selected)
            bundles.append(
                Bundle(
                    id=f"contract:{PurePosixPath(path).stem}",
                    framework=self.name,
                    entry=path,
                    files=sorted(selected),
                    relations=relations_for(selected),
                    score=score,
                )
            )

        bundles.sort(key=lambda item: (item.score, len(item.files)), reverse=True)
        return bundles


class SpringBootAdapter(FrameworkAdapter):
    """Spring Boot 适配器骨架。

    当前只做：
    - 识别 @RestController / @Controller
    - 用 import 建立简单的类依赖
    - 生成 Controller 审查包

    真正生产化需要 JavaParser / Eclipse JDT / Maven 依赖解析。
    """

    name = "spring-boot"

    def detect(self, files: list[dict]) -> bool:
        java_files = [item for item in files if item["path"].endswith(".java")]
        if not java_files:
            return False
        text = "\n".join(item["content"][:4000] for item in java_files[:50])
        return any(
            marker in text
            for marker in ("@SpringBootApplication", "@RestController", "@Controller")
        )

    def build_bundles(self, files: list[dict]) -> list[Bundle]:
        by_path = {item["path"]: item for item in files}
        class_to_path: dict[str, str] = {}
        for path, item in by_path.items():
            if not path.endswith(".java"):
                continue
            match = re.search(
                r"\b(?:class|interface|enum)\s+([A-Za-z_]\w*)",
                item["content"],
            )
            if match:
                class_to_path.setdefault(match.group(1), path)

        forward: dict[str, set[str]] = {path: set() for path in by_path}
        for path, item in by_path.items():
            if not path.endswith(".java"):
                continue
            for class_name in re.findall(
                r"^\s*import\s+[\w.]+\.([A-Za-z_]\w*)\s*;",
                item["content"],
                flags=re.MULTILINE,
            ):
                target = class_to_path.get(class_name)
                if target and target != path:
                    forward[path].add(target)

        bundles: list[Bundle] = []
        for path, item in by_path.items():
            if not path.endswith(".java"):
                continue
            if "@RestController" not in item["content"] and "@Controller" not in item["content"]:
                continue
            selected = {path} | forward[path]
            relations = [
                {"from": path, "to": target, "type": "import"}
                for target in forward[path]
            ]
            score = sum(by_path[item_path].get("score", 0) for item_path in selected) + 5
            bundles.append(
                Bundle(
                    id=f"controller:{PurePosixPath(path).stem}",
                    framework=self.name,
                    entry=path,
                    files=sorted(selected),
                    relations=relations,
                    score=score,
                    experimental=True,
                )
            )

        bundles.sort(key=lambda item: item.score, reverse=True)
        return bundles


def build_bundles(files: list[dict]) -> list[Bundle]:
    adapters: list[FrameworkAdapter] = [
        SpringBootAdapter(),
        PythonPytestAdapter(),
    ]
    for adapter in adapters:
        if adapter.detect(files):
            return adapter.build_bundles(files)
    return []
