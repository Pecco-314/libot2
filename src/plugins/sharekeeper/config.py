from pydantic import BaseModel


class Config(BaseModel):
    bilibili_share_keeper_redis_url: str = "redis://localhost:6379/0"
    bilibili_share_keeper_target_group: int = 605275779
    bilibili_share_keeper_key_prefix: str = "nb2:bili_share_keeper"
    bilibili_share_keeper_http_timeout: float = 10.0

