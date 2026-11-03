"""本机/docs练习：临时MySQL+假网关，退出后清理测试数据，不调用真实模型。"""

import argparse

import httpx
from openai import APITimeoutError
import uvicorn

from app.config import get_mysql_password
from scripts._chat_api_test_app import temporary_chat_api


def main():
    parser = argparse.ArgumentParser(description='启动本机假模型聊天接口练习应用，退出时清理独立临时库。')
    parser.add_argument('--port', type=int, default=8001)
    parser.add_argument('--simulate-timeout', action='store_true', help='模拟首次发送超时，随后可练习重试接口')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须为1～65535。')
    password = get_mysql_password()
    with temporary_chat_api(password) as (app, state):
        if args.simulate_timeout:
            state['builder'].next_errors.append(APITimeoutError(httpx.Request('POST', 'https://example.invalid/chat')))
        print(f'假模型练习文档：http://127.0.0.1:{args.port}/docs')
        print('账号、聊天与配置都在本次临时库中；请使用假Key。Ctrl+C退出后清理。')
        uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)
    print('练习应用已退出，本次临时库已清理。')


if __name__ == '__main__':
    main()
