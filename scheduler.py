import httpx
import asyncio
from database import get_db, get_sys_config
from logger import add_log

# ---------------- 清晰度评分权重 ----------------
QUALITY_MAP = {
    "4k": 100, "2160p": 100, "uhd": 100,
    "1080p": 80, "fhd": 80, "bdrip": 75,
    "720p": 60, "hd": 60,
    "dvd": 40, "remux": 95
}

def get_quality_score(text: str) -> int:
    """根据文件名或说明文本计算清晰度得分"""
    text = text.lower()
    score = 50 # 基础分
    for key, weight in QUALITY_MAP.items():
        if key in text:
            score = max(score, weight)
    return score

# ---------------- 115 查重与对比逻辑 ----------------
async def check_115_existing_quality(cookie: str, title: str):
    """
    搜索 115 网盘，检查是否已存在该影片及其清晰度得分
    """
    if not cookie:
        return None, 0
    
    # 115 搜索接口 (需配合有效的 115 Cookie)
    search_url = f"https://webapi.115.com/files/search?search_value={title}"
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(search_url, headers=headers)
            res_data = res.json()
            if res_data.get("state") and res_data.get("data"):
                file_list = res_data["data"]
                if not file_list:
                    return None, 0
                
                # 获取搜索结果中最高清晰度的文件
                best_match = None
                max_score = 0
                for f in file_list:
                    name = f.get("n", "")
                    score = get_quality_score(name)
                    if score > max_score:
                        max_score = score
                        best_match = name
                return best_match, max_score
        except Exception as e:
            add_log("ERROR", f"【115查重】访问 115 接口异常: {str(e)}")
    return None, 0

# ---------------- CMS 推送核心逻辑 ----------------
async def push_to_cms(cms_url: str, cms_token: str, link: str):
    """
    精准对接 CloudMediaSynC 官方接口
    POST /api/cloud/add_share_down_by_token
    """
    api_endpoint = f"{cms_url.rstrip('/')}/api/cloud/add_share_down_by_token"
    
    # 严格按照文档要求的 JSON 结构
    payload = {
        "url": link,
        "token": cms_token
    }
    
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            # 官方文档要求 POST 请求
            res = await client.post(api_endpoint, json=payload)
            res_json = res.json()
            
            # 响应校验: { "code": 200, "msg": "添加转存下载任务成功" }
            if res_json.get("code") == 200:
                return True, res_json.get("msg")
            else:
                return False, res_json.get("msg", "未知错误")
        except Exception as e:
            return False, f"连接 CMS 失败: {str(e)}"

# ---------------- 自动订阅调度主任务 ----------------
async def auto_subscription_task():
    """定时任务核心：检索 -> 查重 -> 评分 -> 推送"""
    add_log("INFO", "【定时任务】启动自动检索与推送订阅任务...")
    
    config = get_sys_config()
    pansou_domain = config.get('pansou_domain', "http://192.168.68.200:8080")
    cms_url = config.get('cms_api_url')
    cms_token = config.get('cms_api_token')
    cookie_115 = config.get('cookie_115')
    
    if not cms_url or not cms_token:
        add_log("WARN", "【定时任务】CMS 地址或 Token 未配置，已跳过。")
        return

    # 获取待处理订阅
    conn = get_db()
    subs = conn.execute('''SELECT s.tmdb_id, m.title FROM subscriptions s 
                           JOIN media_items m ON s.tmdb_id = m.tmdb_id 
                           WHERE s.status = 'pending' ''').fetchall()
    conn.close()

    if not subs:
        add_log("INFO", "【定时任务】当前没有待处理的订阅。")
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
        for sub in subs:
            tmdb_id, title = sub['tmdb_id'], sub['title']
            add_log("INFO", f"【搜刮】开始执行: 《{title}》")
            
            try:
                # 1. 调用盘搜
                ps_res = await client.post(f"{pansou_domain.rstrip('/')}/api/search", json={"kw": title})
                data = ps_res.json().get("data", {}).get("merged_by_type", {})
                
                # 2. 优先级筛选: 115 -> magnet -> ed2k -> aliyun
                priorities = ["115", "magnet", "ed2k", "aliyun"]
                best_link, hit_type, new_resource_note = None, None, ""
                
                for p_type in priorities:
                    links = data.get(p_type, [])
                    if links:
                        best_link = links[0]["url"]
                        hit_type = p_type
                        new_resource_note = links[0].get("note", "")
                        break
                
                if best_link:
                    # 3. 115 查重与清晰度对比
                    existing_file, existing_score = await check_115_existing_quality(cookie_115, title)
                    new_score = get_quality_score(new_resource_note or title)
                    
                    if existing_file:
                        if existing_score >= new_score:
                            add_log("INFO", f"【跳过】网盘已存在相同或更优版本: {existing_file} (得分:{existing_score})")
                            # 标记完成并取消订阅
                            conn = get_db(); conn.execute("DELETE FROM subscriptions WHERE tmdb_id = ?", (tmdb_id,)); conn.commit(); conn.close()
                            continue
                        else:
                            add_log("WARN", f"【升级】网盘已有版本分较低({existing_score})，发现更高清版本({new_score})，准备替换推送...")

                    # 4. 推送至 CMS
                    add_log("INFO", f"【推送】命中《{title}》的 {hit_type} 资源，准备推送至 CMS...")
                    success, msg = await push_to_cms(cms_url, cms_token, best_link)
                    
                    if success:
                        add_log("SUCCESS", f"【成功】《{title}》{msg}")
                        # 5. 推送成功，取消订阅
                        conn = get_db()
                        conn.execute("DELETE FROM subscriptions WHERE tmdb_id = ?", (tmdb_id,))
                        conn.commit()
                        conn.close()
                    else:
                        add_log("ERROR", f"【失败】CMS 拒绝推送: {msg}")
                else:
                    add_log("WARN", f"【搜刮】全网未找到《{title}》的相关资源。")
            except Exception as e:
                add_log("ERROR", f"【失败】处理异常: {str(e)}")

            await asyncio.sleep(2) # 避免请求过快
            
    add_log("INFO", "【定时任务】本轮自动化流程执行结束。")