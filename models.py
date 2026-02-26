from pydantic import BaseModel
from typing import Optional

class ConfigModel(BaseModel):
    api_domain: str
    image_domain: str
    api_key: str
    pansou_domain: str
    cron_expression: str
    cms_api_url: str
    cms_api_token: str

class SubscribeModel(BaseModel):
    tmdb_id: int
    media_type: str
    title: str
    overview: Optional[str] = ""
    poster_path: Optional[str] = ""
    force: Optional[bool] = False  # 新增：强制订阅标识

class QrcodeStatusModel(BaseModel):
    uid: str
    time: int
    sign: str

class QrcodeLoginModel(BaseModel):
    uid: str