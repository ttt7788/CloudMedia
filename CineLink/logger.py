import sqlite3
import datetime
from database import get_db

def add_log(level: str, message: str):
    """写入系统日志到数据库"""
    conn = get_db()
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO system_logs (level, message, created_at) VALUES (?, ?, ?)", 
                     (level, message, now))
        conn.commit()
    except Exception as e:
        print(f"写入日志失败: {e}")
    finally:
        conn.close()

def get_logs(limit: int = 100):
    """获取最新日志"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM system_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]