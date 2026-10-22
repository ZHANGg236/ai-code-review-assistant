"""FrameworkAdapter / Bundle 骨架的独立验证脚本。

运行：
    python test_bundles.py

不依赖 pytest，也不会调用模型。
"""

from review_bundles import build_bundles

PYTHON_FILES = [
    {
        "path": "conftest.py",
        "content": (
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def api_client():\n"
            "    return object()\n"
        ),
        "score": 1,
        "lines": 6,
        "language": "python",
    },
    {
        "path": "tests/test_login.py",
        "content": (
            "from pages.login_page import LoginPage\n\n"
            "def test_login(api_client):\n"
            "    assert LoginPage(api_client)\n"
        ),
        "score": 4,
        "lines": 4,
        "language": "python",
    },
    {
        "path": "pages/login_page.py",
        "content": (
            "from utils.http_client import HttpClient\n\n"
            "class LoginPage:\n"
            "    def __init__(self, client):\n"
            "        self.client = HttpClient(client)\n"
        ),
        "score": 3,
        "lines": 5,
        "language": "python",
    },
    {
        "path": "utils/http_client.py",
        "content": (
            "class HttpClient:\n"
            "    def __init__(self, client):\n"
            "        self.client = client\n"
        ),
        "score": 2,
        "lines": 3,
        "language": "python",
    },
]

JAVA_FILES = [
    {
        "path": "src/main/java/com/demo/UserController.java",
        "content": (
            "package com.demo;\n"
            "import com.demo.UserService;\n"
            "@RestController\n"
            "public class UserController { UserService service; }\n"
        ),
        "score": 5,
        "lines": 4,
        "language": "java",
    },
    {
        "path": "src/main/java/com/demo/UserService.java",
        "content": (
            "package com.demo;\n"
            "@Service\n"
            "public class UserService { }\n"
        ),
        "score": 2,
        "lines": 3,
        "language": "java",
    },
]


def test_python_bundle() -> None:
    bundles = build_bundles(PYTHON_FILES)
    assert bundles, "Python adapter 没有生成 bundle"

    test_bundle = next(
        (item for item in bundles if item.id == "test:test_login"),
        None,
    )
    assert test_bundle is not None, "没有生成 test_login bundle"
    assert test_bundle.framework == "python-pytest"
    assert "tests/test_login.py" in test_bundle.files
    assert "pages/login_page.py" in test_bundle.files
    assert "utils/http_client.py" in test_bundle.files
    assert "conftest.py" in test_bundle.files
    assert test_bundle.relations, "没有生成 import 关系"


def test_spring_bundle() -> None:
    bundles = build_bundles(JAVA_FILES)
    assert bundles, "Spring Boot adapter 没有生成 bundle"

    controller = next(
        (item for item in bundles if item.id == "controller:UserController"),
        None,
    )
    assert controller is not None, "没有生成 UserController bundle"
    assert controller.framework == "spring-boot"
    assert controller.experimental is True
    assert "src/main/java/com/demo/UserService.java" in controller.files


if __name__ == "__main__":
    test_python_bundle()
    test_spring_bundle()
    print("FrameworkAdapter / Bundle 骨架测试通过")
