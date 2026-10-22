"""SQLite 历史记录与反馈存储。

设计原则：
1. 默认只存代码 hash，不存完整代码。
2. 存审查结果、反馈和摘要，用于后续“历史记忆”。
3. 使用标准库 sqlite3，不引入新依赖。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("REVIEW_DB_PATH", str(DATA_DIR / "reviews.db")))

ALLOWED_VERDICTS = {"helpful", "false_positive", "fixed"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db() -> None:
    connection = _connect()
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                language TEXT NOT NULL,
                mode TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                code_chars INTEGER NOT NULL,
                summary TEXT NOT NULL,
                review_text TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(review_id) REFERENCES reviews(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_reviews_created
                ON reviews(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reviews_language
                ON reviews(language);
            CREATE INDEX IF NOT EXISTS idx_feedback_review
                ON feedback(review_id);
            """
        )
        connection.commit()
    finally:
        connection.close()


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def make_summary(review_text: str) -> str:
    if not review_text:
        return ""
    first_line = review_text.strip().splitlines()[0]
    cleaned = re.sub(r"[#>*_`\[\]]", "", first_line).strip()
    return cleaned[:120]


def save_review(
    *,
    language: str,
    mode: str,
    code: str,
    review_text: str,
) -> int:
    connection = _connect()
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO reviews (
                    created_at, language, mode, code_hash,
                    code_chars, summary, review_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    language,
                    mode,
                    hash_code(code),
                    len(code),
                    make_summary(review_text),
                    review_text,
                ),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def list_reviews(limit: int = 20) -> list[dict]:
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT
                r.id,
                r.created_at,
                r.language,
                r.mode,
                r.summary,
                COUNT(f.id) AS feedback_count,
                (
                    SELECT verdict
                    FROM feedback f2
                    WHERE f2.review_id = r.id
                    ORDER BY f2.id DESC
                    LIMIT 1
                ) AS latest_verdict
            FROM reviews r
            LEFT JOIN feedback f ON f.review_id = r.id
            GROUP BY r.id
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_review(review_id: int) -> dict | None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            return None

        feedback_rows = connection.execute(
            """
            SELECT id, verdict, comment, created_at
            FROM feedback
            WHERE review_id = ?
            ORDER BY id DESC
            """,
            (review_id,),
        ).fetchall()

        result = dict(row)
        result["feedback"] = [dict(item) for item in feedback_rows]
        return result
    finally:
        connection.close()


def add_feedback(
    review_id: int,
    verdict: str,
    comment: str = "",
) -> bool:
    if verdict not in ALLOWED_VERDICTS:
        return False

    connection = _connect()
    try:
        exists = connection.execute(
            "SELECT 1 FROM reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
        if exists is None:
            return False

        with connection:
            connection.execute(
                """
                INSERT INTO feedback (review_id, verdict, comment, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (review_id, verdict, comment[:500], _now()),
            )
        return True
    finally:
        connection.close()


def get_insights() -> dict:
    connection = _connect()
    try:
        total = connection.execute(
            "SELECT COUNT(*) AS count FROM reviews"
        ).fetchone()["count"]

        last_7_days = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM reviews
            WHERE created_at >= datetime('now', '-7 days')
            """
        ).fetchone()["count"]

        by_language = [
            dict(row)
            for row in connection.execute(
                """
                SELECT language, COUNT(*) AS count
                FROM reviews
                GROUP BY language
                ORDER BY count DESC
                """
            ).fetchall()
        ]

        verdict_rows = connection.execute(
            """
            SELECT verdict, COUNT(*) AS count
            FROM feedback
            GROUP BY verdict
            """
        ).fetchall()
        verdicts = {row["verdict"]: row["count"] for row in verdict_rows}

        return {
            "total_reviews": total,
            "reviews_last_7_days": last_7_days,
            "by_language": by_language,
            "feedback": {
                "helpful": verdicts.get("helpful", 0),
                "false_positive": verdicts.get("false_positive", 0),
                "fixed": verdicts.get("fixed", 0),
            },
        }
    finally:
        connection.close()


def get_memory_summary(language: str) -> str:
    """生成一段简短的团队历史反馈，注入 Prompt。"""
    since = (
        datetime.now(timezone.utc) - timedelta(days=90)
    ).strftime("%Y-%m-%d %H:%M:%S")

    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT f.verdict, COUNT(*) AS count
            FROM feedback f
            JOIN reviews r ON r.id = f.review_id
            WHERE r.language = ? AND r.created_at >= ?
            GROUP BY f.verdict
            """,
            (language, since),
        ).fetchall()
    finally:
        connection.close()

    counts = {row["verdict"]: row["count"] for row in rows}
    helpful = counts.get("helpful", 0)
    false_positive = counts.get("false_positive", 0)
    fixed = counts.get("fixed", 0)

    if not any((helpful, false_positive, fixed)):
        return ""

    lines = [
        f"最近 90 天，{language} 审查反馈："
        f"有用 {helpful} 次，误报 {false_positive} 次，已修复 {fixed} 次。"
    ]
    if false_positive:
        lines.append(
            "对于该语言下不确定的问题，请降低置信度，避免重复误报。"
        )
    if fixed:
        lines.append(
            "该团队会实际采纳修复建议，请给出可直接执行的修改。"
        )
    return "\n".join(lines)
