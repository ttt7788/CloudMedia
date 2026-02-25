import os
import contextlib
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import init_db, get_sys_config
from api_routes import router
from scheduler import auto_subscription_task
from logger import add_log

# 全局调度器
scheduler = AsyncIOScheduler()

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # --- 启动时执行 ---
    init_db()
    config = get_sys_config()
    cron_expr = config.get("cron_expression", "0 * * * *")
    
    try:
        # 解析 cron: 分 时 日 月 周
        parts = cron_expr.split()
        if len(parts) == 5:
            trigger = CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4])
            scheduler.add_job(auto_subscription_task, trigger, id="auto_sub_task", replace_existing=True)
            scheduler.start()
            add_log("INFO", f"【系统启动】已加载定时任务，Cron表达式: {cron_expr}")
        else:
            add_log("WARN", "【系统启动】Cron 表达式格式错误，未能启动定时任务。")
    except Exception as e:
        add_log("ERROR", f"【系统启动】定时任务启动失败: {str(e)}")
        
    yield
    # --- 停止时执行 ---
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(router)

@app.get("/")
def read_root(): return FileResponse("index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)