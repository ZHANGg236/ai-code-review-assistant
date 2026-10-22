"""评测用例：每道题包含一段代码和期望出现的关键词。

关键词匹配只是工程冒烟测试，不等同于严格的准确率评测。
它的价值在于：每次修改 Prompt 或模型后，可以快速发现明显的回归。
"""

CASES = [
    {
        "id": "sql-injection",
        "title": "SQL 注入",
        "language": "python",
        "code": """def get_user(username):
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    return db.execute(query)""",
        "expected": ["SQL 注入", "SQL注入", "注入", "参数化"],
    },
    {
        "id": "xss",
        "title": "XSS",
        "language": "javascript",
        "code": """function renderComment(comment) {
  document.getElementById("comment").innerHTML = comment;
}""",
        "expected": ["XSS", "跨站脚本", "转义", "textContent"],
    },
    {
        "id": "hardcoded-secret",
        "title": "硬编码密钥",
        "language": "python",
        "code": """API_KEY = "sk-live-1234567890abcdef"


def call_api():
    return requests.get(
        "https://api.example.com",
        headers={"Authorization": API_KEY},
    )""",
        "expected": ["硬编码", "密钥", "secret", "环境变量"],
    },
    {
        "id": "command-injection",
        "title": "命令注入",
        "language": "python",
        "code": """import os


def ping(host):
    os.system("ping -c 1 " + host)""",
        "expected": ["命令注入", "命令执行", "shell", "subprocess", "参数化"],
    },
    {
        "id": "path-traversal",
        "title": "路径遍历",
        "language": "python",
        "code": """def read_file(filename):
    path = "/var/www/uploads/" + filename
    with open(path, "rb") as f:
        return f.read()""",
        "expected": ["路径遍历", "路径穿越", "目录遍历", "basename", "白名单"],
    },
    {
        "id": "weak-hash",
        "title": "弱哈希",
        "language": "python",
        "code": """import hashlib


def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()""",
        "expected": ["MD5", "弱哈希", "bcrypt", "argon2", "salt", "加盐"],
    },
    {
        "id": "n-plus-one",
        "title": "N+1 查询",
        "language": "python",
        "code": """def get_users_with_orders():
    users = User.objects.all()
    result = []
    for user in users:
        result.append({
            "user": user,
            "orders": Order.objects.filter(user=user),
        })
    return result""",
        "expected": ["N+1", "N + 1", "循环查询", "select_related", "prefetch_related", "批量"],
    },
    {
        "id": "race-condition",
        "title": "竞态条件",
        "language": "python",
        "code": """counter = 0


def increment():
    global counter
    value = counter
    counter = value + 1""",
        "expected": ["竞态", "race", "线程安全", "锁", "Lock", "原子"],
    },
    {
        "id": "insecure-deserialization",
        "title": "不安全的反序列化",
        "language": "python",
        "code": """import pickle


def load_session(data):
    return pickle.loads(data)""",
        "expected": ["反序列化", "pickle", "任意代码", "不可信"],
    },
    {
        "id": "resource-leak",
        "title": "资源泄漏",
        "language": "python",
        "code": """def read_config(path):
    f = open(path)
    data = f.read()
    return data""",
        "expected": ["资源泄漏", "with", "关闭", "close", "上下文管理器"],
    },
]
