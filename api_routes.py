import httpx
import datetime
from fastapi import APIRouter, HTTPException
from database import get_db, get_sys_config
from models import ConfigModel, SubscribeModel, QrcodeStatusModel, QrcodeLoginModel
from logger import get_logs, add_log

router = APIRouter()

# ---------------- 1. 系统配置管理 ----------------
@router.get("/api/config")
def get_config():
    """获取数据库中存储的所有配置项"""
    return get_sys_config()

@router.post("/api/config")
def update_config(config: ConfigModel):
    """保存配置"""
    conn = get_db()
    try:
        fields = [
            ('api_domain', config.api_domain),
            ('image_domain', config.image_domain),
            ('api_key', config.api_key),
            ('pansou_domain', config.pansou_domain),
            ('cron_expression', config.cron_expression),
            ('cms_api_url', config.cms_api_url),
            ('cms_api_token', config.cms_api_token)
        ]
        for key, value in fields:
            conn.execute("REPLACE INTO system_configs (config_key, config_value) VALUES (?, ?)", (key, value))
        conn.commit()
        return {"message": "配置保存成功，定时任务修改后建议重启服务"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")
    finally:
        conn.close()

# ---------------- 2. 影视数据采集与订阅 ----------------
@router.get("/api/sync")
async def sync_daily_data():
    """每日自动采集 TMDB 热门数据"""
    config = get_sys_config()
    today_str = datetime.date.today().isoformat()
    if config.get('last_sync_date') == today_str:
        return {"status": "skipped", "message": "今日最新数据已入库"}

    api_key = config.get('api_key')
    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 API Key")

    async with httpx.AsyncClient() as client:
        try:
            res_movie = await client.get(f"{config['api_domain']}/3/trending/movie/day", params={"api_key": api_key, "language": "zh-CN"})
            res_tv = await client.get(f"{config['api_domain']}/3/trending/tv/day", params={"api_key": api_key, "language": "zh-CN"})
            
            items = []
            if res_movie.status_code == 200: items.extend(res_movie.json().get('results', []))
            if res_tv.status_code == 200: items.extend(res_tv.json().get('results', []))
                
            conn = get_db()
            cursor = conn.cursor()
            for item in items:
                media_type = item.get('media_type', 'movie')
                title = item.get('title') or item.get('name')
                cursor.execute('''INSERT OR REPLACE INTO media_items (tmdb_id, media_type, title, overview, poster_path, add_date) 
                                  VALUES (?, ?, ?, ?, ?, ?)''', (item['id'], media_type, title, item.get('overview'), item.get('poster_path'), today_str))
                
            cursor.execute("REPLACE INTO system_configs (config_key, config_value) VALUES ('last_sync_date', ?)", (today_str,))
            conn.commit()
            conn.close()
            return {"status": "success", "message": f"成功自动同步 {len(items)} 条数据"}
        except Exception as e:
            add_log("ERROR", f"每日同步失败: {str(e)}")

@router.get("/api/local_media")
def get_local_media(type: str = 'hot'):
    """查询本地数据库存储的影视资源"""
    conn = get_db()
    sub_ids = [row['tmdb_id'] for row in conn.execute("SELECT tmdb_id FROM subscriptions").fetchall()]
    
    query = "SELECT * FROM media_items ORDER BY add_date DESC, tmdb_id DESC LIMIT 60"
    if type == 'movie': query = "SELECT * FROM media_items WHERE media_type='movie' ORDER BY add_date DESC, tmdb_id DESC LIMIT 60"
    elif type == 'tv': query = "SELECT * FROM media_items WHERE media_type='tv' ORDER BY add_date DESC, tmdb_id DESC LIMIT 60"
    
    rows = conn.execute(query).fetchall()
    conn.close()
    return [{**dict(row), 'is_subscribed': row['tmdb_id'] in sub_ids} for row in rows]

@router.get("/api/search")
async def search_tmdb(query: str):
    """代理搜索 TMDB 网络资源"""
    config = get_sys_config()
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{config['api_domain']}/3/search/multi", params={"api_key": config['api_key'], "query": query, "language": "zh-CN"})
        data = res.json()
        conn = get_db()
        sub_ids = [r[0] for r in conn.execute("SELECT tmdb_id FROM subscriptions").fetchall()]
        conn.close()
        for i in data.get('results', []): i['is_subscribed'] = i.get('id') in sub_ids
        return data

@router.post("/api/subscribe")
def subscribe(media: SubscribeModel):
    """添加订阅到本地任务队列"""
    conn = get_db()
    today = datetime.date.today().isoformat()
    conn.execute("INSERT OR REPLACE INTO media_items (tmdb_id, media_type, title, overview, poster_path, add_date) VALUES (?,?,?,?,?,?)", 
                 (media.tmdb_id, media.media_type, media.title, media.overview, media.poster_path, today))
    conn.execute("INSERT OR IGNORE INTO subscriptions (tmdb_id) VALUES (?)", (media.tmdb_id,))
    conn.commit()
    conn.close()
    return {"message": "订阅成功"}

@router.get("/api/subscriptions")
def get_subscriptions():
    """获取当前所有待处理和处理中的订阅"""
    conn = get_db()
    rows = conn.execute("SELECT s.status, m.* FROM subscriptions s JOIN media_items m ON s.tmdb_id = m.tmdb_id ORDER BY s.id DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]

@router.delete("/api/subscriptions/{tmdb_id}")
def unsubscribe(tmdb_id: int):
    """取消订阅"""
    conn = get_db()
    conn.execute("DELETE FROM subscriptions WHERE tmdb_id = ?", (tmdb_id,))
    conn.commit()
    conn.close()
    return {"message": "已取消订阅"}

# ---------------- 3. 网盘搜索与 115 ----------------
@router.get("/api/pansou_search")
async def search_ps(kw: str):
    """代理 PanSou 搜索"""
    c = get_sys_config()
    domain = c.get('pansou_domain', 'http://192.168.68.200:8080').rstrip('/')
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(f"{domain}/api/search", json={"kw": kw})
            d = res.json()
            return d.get("data") if d.get("code") == 0 else d
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"盘搜连接失败: {str(e)}")

@router.get("/api/115/qrcode")
async def get_115_qr():
    """获取 115 登录二维码 Token"""
    async with httpx.AsyncClient() as client:
        return (await client.get("https://qrcodeapi.115.com/api/1.0/web/1.0/token/")).json()

@router.post("/api/115/status")
async def get_115_st(p: QrcodeStatusModel):
    """查询 115 扫码状态"""
    async with httpx.AsyncClient() as client:
        return (await client.get(f"https://qrcodeapi.115.com/get/status/?uid={p.uid}&time={p.time}&sign={p.sign}")).json()

@router.post("/api/115/login")
async def log_115(p: QrcodeLoginModel):
    """扫码成功后换取 Cookie"""
    async with httpx.AsyncClient() as client:
        res = (await client.post("https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/", data={"app": "web", "account": p.uid})).json()
        if res.get('state'):
            ck = "; ".join(f"{k}={v}" for k, v in res['data']['cookie'].items())
            conn = get_db()
            conn.execute("REPLACE INTO system_configs (config_key, config_value) VALUES ('cookie_115', ?)", (ck,))
            conn.commit()
            conn.close()
            return {"message": "115 登录成功"}
    raise HTTPException(400, "登录失败")

# ---------------- 4. 系统工具与任务 ----------------
@router.get("/api/logs")
def fetch_logs():
    """查询系统日志"""
    return get_logs(100)

@router.post("/api/tasks/trigger")
async def trigger_task():
    """手动触发后台定时任务脚本"""
    from scheduler import auto_subscription_task
    import asyncio
    asyncio.create_task(auto_subscription_task())
    return {"message": "后台搜刮与推送任务已启动"}