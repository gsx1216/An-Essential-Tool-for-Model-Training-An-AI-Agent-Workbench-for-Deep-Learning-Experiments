"""开发启动入口；可交互输入 Redis 密码，仅用于本次进程，不写配置文件。"""

import argparse
from getpass import getpass
import os

import uvicorn

from app.config import get_event_bus_settings
from app.models.event_bus import with_redis_password


def main():
    parser = argparse.ArgumentParser(description="启动实验助手 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--prompt-redis-password", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须为 1～65535。")
    if args.prompt_redis_password:
        settings = with_redis_password(get_event_bus_settings(), getpass("请输入 Redis 密码（仅本次进程使用）："))
        os.environ["REDIS_URL"] = settings.redis_url
    uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
