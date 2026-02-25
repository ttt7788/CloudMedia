import sqlite3

def init_db():
    conn = sqlite3.connect('tmdb_system.db')
    cursor = conn.cursor()
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS system_configs (config_key VARCHAR(50) UNIQUE PRIMARY KEY, config_value VARCHAR(255))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS media_items (tmdb_id INTEGER PRIMARY KEY, media_type VARCHAR(20), title VARCHAR(255), overview TEXT, poster_path VARCHAR(255))''')
    try: cursor.execute("ALTER TABLE media_items ADD COLUMN add_date DATE")
    except sqlite3.OperationalError: pass 
    cursor.execute('''CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER UNIQUE, status VARCHAR(20) DEFAULT 'pending')''')
    
    # 【新增】系统日志表
    cursor.execute('''CREATE TABLE IF NOT EXISTS system_logs 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, level VARCHAR(20), message TEXT, created_at DATETIME)''')
    
    default_configs = [
        ('api_key', '5ac680068ecbeded86de5c9cca4bdf70'), ('api_domain', 'https://api.tmdb.org'),
        ('image_domain', 'https://image.tmdb.org'), ('pansou_domain', 'http://192.168.68.200:8080'),
        ('last_sync_date', ''), ('cookie_115', ''),
        # 【新增】定时任务与 CMS 对接配置
        ('cron_expression', '0 * * * *'),       # 默认每小时执行一次: 分 时 日 月 周
        ('cms_api_url', 'http://127.0.0.1:8090'), # CMS 地址
        ('cms_api_token', '')                    # CMS 认证 Token
    ]
    cursor.executemany('INSERT OR IGNORE INTO system_configs (config_key, config_value) VALUES (?, ?)', default_configs)
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect('tmdb_system.db')
    conn.row_factory = sqlite3.Row
    return conn

def get_sys_config():
    conn = get_db()
    rows = conn.execute("SELECT config_key, config_value FROM system_configs").fetchall()
    conn.close()
    return {row['config_key']: row['config_value'] for row in rows}