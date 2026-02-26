import httpx
import asyncio
import datetime
import random
from database import get_db, get_sys_config
from logger import add_log

QUALITY_MAP = {
    "4k": 100, "2160p": 100, "uhd": 100,
    "1080p": 80, "fhd": 80, "bdrip": 75,
    "720p": 60, "hd": 60,
    "dvd": 40, "remux": 95
}

def get_quality_score(text: str) -> int:
    text = text.lower()
    score = 50 
    for key, weight in QUALITY_MAP.items():
        if key in text: score = max(score, weight)
    return score

async def check_115_existing_quality(cookie: str, title: str):
    if not cookie: return None, 0
    await asyncio.sleep(random.uniform(0.2, 0.5))
    search_url = f"https://webapi.115.com/files/search?search_value={title}"
    headers = {"Cookie": cookie, "User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(search_url, headers=headers)
            res_data = res.json()
            if res_data.get("state") and res_data.get("data"):
                file_list = res_data["data"]
                if not file_list: return None, 0
                best_match, max_score = None, 0
                for f in file_list:
                    name = f.get("n", "")
                    score = get_quality_score(name)
                    if score > max_score:
                        max_score = score; best_match = name
                return best_match, max_score
        except Exception: pass
    return None, 0

async def push_to_cms(cms_url: str, cms_token: str, link: str):
    api_endpoint = f"{cms_url.rstrip('/')}/api/cloud/add_share_down_by_token"
    payload = {"url": link, "token": cms_token}
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            res = await client.post(api_endpoint, json=payload)
            res_json = res.json()
            if res_json.get("code") == 200: return True, res_json.get("msg")
            return False, res_json.get("msg", "未知错误")
        except Exception as e:
            return False, f"连接 CMS 失败: {str(e)}"

async def sync_tmdb_data(force=False):
    config = get_sys_config()
    api_key = config.get('api_key')
    if not api_key: return

    today_str = datetime.date.today().isoformat()
    if not force and config.get('last_sync_date') == today_str: return 

    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
    conn.close()

    base_url = config.get('api_domain', 'https://api.tmdb.org').rstrip('/')
    items = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if count < 15000:
                add_log("INFO", f"【库同步】本地库不足({count}条)，启动并发大补全...")
                sem = asyncio.Semaphore(15) 
                async def fetch_page(m_type, page):
                    async with sem:
                        try:
                            res = await client.get(f"{base_url}/3/{m_type}/popular", params={"api_key": api_key, "language": "zh-CN", "page": page})
                            if res.status_code == 200:
                                res_items = res.json().get('results', [])
                                for r in res_items: r['media_type'] = m_type
                                return res_items
                        except Exception: pass
                        return []

                tasks_m = [fetch_page('movie', p) for p in range(1, 501)]
                for i in range(0, 500, 100):
                    res_list = await asyncio.gather(*tasks_m[i:i+100])
                    for r in res_list: items.extend(r)
                    add_log("INFO", f"【库同步】电影库已处理 {min(i+100, 500)} 页...")

                tasks_t = [fetch_page('tv', p) for p in range(1, 501)]
                for i in range(0, 500, 100):
                    res_list = await asyncio.gather(*tasks_t[i:i+100])
                    for r in res_list: items.extend(r)
                    add_log("INFO", f"【库同步】剧集库已处理 {min(i+100, 500)} 页...")
            else:
                add_log("INFO", f"【库同步】本地库充实({count}条)，执行每日热门增量更新...")
            
            for page in range(1, 6):
                try:
                    res_m = await client.get(f"{base_url}/3/trending/movie/day", params={"api_key": api_key, "language": "zh-CN", "page": page})
                    res_t = await client.get(f"{base_url}/3/trending/tv/day", params={"api_key": api_key, "language": "zh-CN", "page": page})
                    if res_m.status_code == 200:
                        for m in res_m.json().get('results', []): m['media_type'] = 'movie'; items.append(m)
                    if res_t.status_code == 200:
                        for t in res_t.json().get('results', []): t['media_type'] = 'tv'; items.append(t)
                except Exception: pass

            unique_items = {item['id']: item for item in items if item.get('id')}.values()
            if not unique_items: return

            insert_data = []
            for item in unique_items:
                title = item.get('title') or item.get('name')
                poster = item.get('poster_path')
                if not title or not poster: continue
                insert_data.append((item['id'], item.get('media_type', 'movie'), title, item.get('overview', ''), poster, today_str))

            conn = get_db()
            cursor = conn.cursor()
            cursor.executemany('''INSERT OR REPLACE INTO media_items (tmdb_id, media_type, title, overview, poster_path, add_date) VALUES (?, ?, ?, ?, ?, ?)''', insert_data)
            cursor.execute("REPLACE INTO system_configs (config_key, config_value) VALUES ('last_sync_date', ?)", (today_str,))
            conn.commit()
            conn.close()
        except Exception as e:
            add_log("ERROR", f"【库同步】严重异常: {str(e)}")

async def auto_subscription_task():
    await sync_tmdb_data(force=False)
    
    add_log("INFO", "【定时任务】开始处理待搜刮的订阅任务...")
    config = get_sys_config()
    pansou_domain = config.get('pansou_domain', "http://192.168.68.200:8080")
    cms_url = config.get('cms_api_url')
    cms_token = config.get('cms_api_token')
    cookie_115 = config.get('cookie_115')
    if not cms_url or not cms_token: return

    conn = get_db()
    subs = conn.execute("SELECT s.tmdb_id, m.title FROM subscriptions s JOIN media_items m ON s.tmdb_id = m.tmdb_id WHERE s.status = 'pending'").fetchall()
    conn.close()
    if not subs: return

    async with httpx.AsyncClient(timeout=30.0) as client:
        for sub in subs:
            tmdb_id, title = sub['tmdb_id'], sub['title']
            add_log("INFO", f"【搜刮】执行中: 《{title}》")
            try:
                ps_res = await client.post(f"{pansou_domain.rstrip('/')}/api/search", json={"kw": title})
                data = ps_res.json().get("data", {}).get("merged_by_type", {})
                
                priorities = ["115", "aliyun", "ed2k", "magnet"]
                best_link, hit_type, new_note = None, None, ""
                for p_type in priorities:
                    if data.get(p_type):
                        best_link, hit_type, new_note = data[p_type][0]["url"], p_type, data[p_type][0].get("note", "")
                        break
                
                if best_link:
                    ex_file, ex_score = await check_115_existing_quality(cookie_115, title)
                    new_score = get_quality_score(new_note or title)
                    if ex_file and ex_score >= new_score:
                        add_log("INFO", f"【跳过】网盘已有极佳版本: {ex_file}")
                        # 核心修改：改为更新状态为成功，放入记录库
                        conn = get_db(); conn.execute("UPDATE subscriptions SET status='success' WHERE tmdb_id=?", (tmdb_id,)); conn.commit(); conn.close()
                        continue

                    success, msg = await push_to_cms(cms_url, cms_token, best_link)
                    if success:
                        add_log("SUCCESS", f"【推送成功】《{title}》至 CMS ({hit_type})")
                        # 核心修改：改为更新状态为成功，放入记录库
                        conn = get_db(); conn.execute("UPDATE subscriptions SET status='success' WHERE tmdb_id=?", (tmdb_id,)); conn.commit(); conn.close()
                    else:
                        add_log("ERROR", f"【失败】CMS 拒绝: {msg}")
                else:
                    add_log("WARN", f"【搜刮】无《{title}》资源。")
            except Exception: pass
            await asyncio.sleep(2)