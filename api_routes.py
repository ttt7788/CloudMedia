import httpx
import datetime
from fastapi import APIRouter, HTTPException
from database import get_db, get_sys_config
from models import ConfigModel, SubscribeModel, QrcodeStatusModel, QrcodeLoginModel
from logger import get_logs

router = APIRouter()

@router.get("/api/config")
def get_config(): return get_sys_config()

@router.post("/api/config")
def update_config(config: ConfigModel):
    conn = get_db()
    try:
        fields = [('api_domain', config.api_domain), ('image_domain', config.image_domain),
                  ('api_key', config.api_key), ('pansou_domain', config.pansou_domain),
                  ('cron_expression', config.cron_expression), ('cms_api_url', config.cms_api_url),
                  ('cms_api_token', config.cms_api_token)]
        for key, value in fields:
            conn.execute("REPLACE INTO system_configs (config_key, config_value) VALUES (?, ?)", (key, value))
        conn.commit()
        return {"message": "配置保存成功"}
    finally: conn.close()

@router.get("/api/sync")
async def sync_daily_data():
    import asyncio
    from scheduler import sync_tmdb_data
    asyncio.create_task(sync_tmdb_data(force=True))
    return {"status": "success", "message": "全量数据入库操作已启动"}

@router.get("/api/local_media")
def get_local_media(type: str = 'hot', page: int = 1, size: int = 30):
    conn = get_db()
    today_str = datetime.date.today().isoformat()
    offset = (page - 1) * size
    
    # 获取具体的订阅状态，而非布尔值
    sub_dict = {row['tmdb_id']: row['status'] for row in conn.execute("SELECT tmdb_id, status FROM subscriptions").fetchall()}
    
    if type == 'hot':
        count_query, data_query = "SELECT COUNT(*) FROM media_items WHERE add_date = ?", "SELECT * FROM media_items WHERE add_date = ? ORDER BY add_date DESC, tmdb_id DESC LIMIT ? OFFSET ?"
        params_count, params_data = (today_str,), (today_str, size, offset)
    elif type == 'movie':
        count_query, data_query = "SELECT COUNT(*) FROM media_items WHERE media_type='movie'", "SELECT * FROM media_items WHERE media_type='movie' ORDER BY add_date DESC, tmdb_id DESC LIMIT ? OFFSET ?"
        params_count, params_data = (), (size, offset)
    else:
        count_query, data_query = "SELECT COUNT(*) FROM media_items WHERE media_type='tv'", "SELECT * FROM media_items WHERE media_type='tv' ORDER BY add_date DESC, tmdb_id DESC LIMIT ? OFFSET ?"
        params_count, params_data = (), (size, offset)
        
    total = conn.execute(count_query, params_count).fetchone()[0]
    rows = conn.execute(data_query, params_data).fetchall()
    conn.close()
    
    items = [{**dict(row), 'sub_status': sub_dict.get(row['tmdb_id'])} for row in rows]
    return {"total": total, "items": items}

@router.get("/api/search")
async def search_tmdb(query: str):
    config = get_sys_config()
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{config['api_domain']}/3/search/multi", params={"api_key": config['api_key'], "query": query, "language": "zh-CN"})
        data = res.json()
        conn = get_db()
        sub_dict = {row['tmdb_id']: row['status'] for row in conn.execute("SELECT tmdb_id, status FROM subscriptions").fetchall()}
        conn.close()
        for i in data.get('results', []): i['sub_status'] = sub_dict.get(i.get('id'))
        return data

@router.post("/api/subscribe")
def subscribe(media: SubscribeModel):
    conn = get_db()
    
    # 核心：检查是否已经订阅且前端没有开启“强制订阅”
    existing = conn.execute("SELECT status FROM subscriptions WHERE tmdb_id = ?", (media.tmdb_id,)).fetchone()
    if existing and not media.force:
        conn.close()
        return {"code": 409, "status": existing['status'], "message": "该资源已存在状态，需要确认"}

    today = datetime.date.today().isoformat()
    conn.execute("INSERT OR REPLACE INTO media_items (tmdb_id, media_type, title, overview, poster_path, add_date) VALUES (?,?,?,?,?,?)", 
                 (media.tmdb_id, media.media_type, media.title, media.overview, media.poster_path, today))
    
    if existing:
        conn.execute("UPDATE subscriptions SET status = 'pending' WHERE tmdb_id = ?", (media.tmdb_id,))
    else:
        conn.execute("INSERT INTO subscriptions (tmdb_id, status) VALUES (?, 'pending')", (media.tmdb_id,))
    
    conn.commit()
    conn.close()
    return {"code": 200, "message": "订阅成功，已加入队列"}

@router.get("/api/subscriptions")
def get_subscriptions(status: str = 'pending'):
    conn = get_db()
    # 根据状态筛选：我的订阅(pending) / 转存记录(success)
    rows = conn.execute("SELECT s.status, m.* FROM subscriptions s JOIN media_items m ON s.tmdb_id = m.tmdb_id WHERE s.status = ? ORDER BY s.id DESC", (status,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]

@router.delete("/api/subscriptions/{tmdb_id}")
def unsubscribe(tmdb_id: int):
    conn = get_db(); conn.execute("DELETE FROM subscriptions WHERE tmdb_id = ?", (tmdb_id,)); conn.commit(); conn.close()
    return {"message": "已取消"}

@router.get("/api/pansou_search")
async def search_ps(kw: str):
    c = get_sys_config()
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(f"{c.get('pansou_domain', 'http://192.168.68.200:8080').rstrip('/')}/api/search", json={"kw": kw})
        d = res.json()
        return d.get("data") if d.get("code") == 0 else d

@router.get("/api/115/qrcode")
async def get_115_qr():
    async with httpx.AsyncClient() as client: return (await client.get("https://qrcodeapi.115.com/api/1.0/web/1.0/token/")).json()

@router.post("/api/115/status")
async def get_115_st(p: QrcodeStatusModel):
    async with httpx.AsyncClient() as client: return (await client.get(f"https://qrcodeapi.115.com/get/status/?uid={p.uid}&time={p.time}&sign={p.sign}")).json()

@router.post("/api/115/login")
async def log_115(p: QrcodeLoginModel):
    async with httpx.AsyncClient() as client:
        res = (await client.post("https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/", data={"app": "web", "account": p.uid})).json()
        if res.get('state'):
            ck = "; ".join(f"{k}={v}" for k, v in res['data']['cookie'].items())
            conn = get_db(); conn.execute("REPLACE INTO system_configs (config_key, config_value) VALUES ('cookie_115', ?)", (ck,)); conn.commit(); conn.close()
            return {"message": "登录成功"}
    raise HTTPException(400, "登录失败")

@router.get("/api/logs")
def fetch_logs(): return get_logs(100)

@router.post("/api/tasks/trigger")
async def trigger_task():
    from scheduler import auto_subscription_task
    import asyncio
    asyncio.create_task(auto_subscription_task())
    return {"message": "后台搜刮任务已启动"}