# 评测脚本

这是一个基于关键词命中的**工程冒烟评测**，用来检查 AI 代码审查的输出是否仍然覆盖常见问题。

它不是严格的准确率评测：关键词命中不等于审查结论正确，也不统计误报。它的价值在于：

- 修改 Prompt / 模型后，快速发现明显回归
- 给项目一个可重复的“效果证据”
- 面试时可以讲清楚：你怎么定义、采集和度量 AI 应用的效果

## 运行

服务启动后：

```powershell
cd D:\FastAPI
.\.venv\Scripts\Activate.ps1

python eval/run_eval.py
```

只跑前 3 个用例：

```powershell
python eval/run_eval.py --limit 3
```

服务启用了访问密码：

```powershell
$env:ACCESS_PASSWORD = "你的访问密码"
python eval/run_eval.py
```

测试部署后的地址：

```powershell
python eval/run_eval.py --base-url https://your-domain.com --password 你的密码
```

结果会写入 `eval/results.json`。

## 用例结构

每个用例在 `cases.py` 中：

```python
{
    "id": "sql-injection",
    "title": "SQL 注入",
    "language": "python",
    "code": "...",
    "expected": ["SQL 注入", "参数化"],
}
```

判定逻辑：审查结果中命中 `expected` 里任意一个关键词，就算该用例通过。

## 如何扩展

1. 在 `cases.py` 里增加一个用例
2. 写清楚代码、语言、期望出现的关键词
3. 重新运行 `python eval/run_eval.py`

建议覆盖：

- SQL 注入、XSS、命令注入、路径遍历
- 硬编码密钥、弱哈希、不安全反序列化
- N+1 查询、竞态条件、资源泄漏
- 超长函数、重复代码、异常处理缺失

## 已知局限

- 关键词匹配比较粗糙，模型换个说法就可能漏判
- 只测“有没有提到”，不测“说得对不对”
- 不统计误报，也不做严重级别判断
- 每个用例都会真实调用模型，会产生 token 消耗

如果要进一步升级，可以：

- 用 LLM-as-a-Judge 做二次判定
- 人工标注 50–100 个样本，计算 precision / recall
- 对同一用例做多次采样，统计稳定性
- 记录模型、Prompt 版本、延迟和 token 成本
