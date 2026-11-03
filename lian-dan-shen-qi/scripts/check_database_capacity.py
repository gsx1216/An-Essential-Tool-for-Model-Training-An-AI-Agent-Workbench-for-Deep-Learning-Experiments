"""维护者 CLI：python -m scripts.check_database_capacity --help。"""

import argparse
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
from uuid import uuid4

from mysql.connector import Error as MySQLError

from app.config import get_mysql_password
from app.errors import SessionNotFoundError
from app.logging_config import configure_logging, log_failure
from app.repositories.capacity_repository import CapacityRepository, ROOT
from app.services.database_capacity_service import render_capacity_report


def make_parser():
    parser = argparse.ArgumentParser(description='只读检查表空间、索引及可选的会话统计与查询计划。')
    parser.add_argument('--user-id', type=int, help='与 session-id 一同提供；维护者需指定真实归属')
    parser.add_argument('--session-id', help='要检查的会话 UUID；不通过 CLI 登录普通用户')
    parser.add_argument('--after-seq', type=int, default=0)
    parser.add_argument('--page-size', type=int, default=50, help='EXPLAIN 的分页大小，1～100')
    parser.add_argument('--sample-limit', type=int, default=1000, help='单会话最多统计条数，1～10000')
    parser.add_argument('--max-execution-ms', type=int, default=2000, help='每条 SELECT 超时，100～30000 ms')
    parser.add_argument('--output', type=Path, help='新 Markdown 文件路径；已有文件不会覆盖')
    return parser


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    if (args.user_id is None) != (args.session_id is None):
        parser.error('--user-id 与 --session-id 必须一起提供。')
    configure_logging()
    try:
        report = CapacityRepository(get_mysql_password()).inspect(
            user_id=args.user_id, session_id=args.session_id, after_seq=args.after_seq,
            page_size=args.page_size, sample_limit=args.sample_limit,
            max_execution_ms=args.max_execution_ms)
        markdown = render_capacity_report(report)
        target = args.output or (ROOT / 'data' / 'diagnostics' /
                 f"database_capacity_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as file:
            file.write(markdown)
    except (ValueError, MySQLError, OSError) as error:
        log_failure(logging.getLogger('app.scripts.check_database_capacity'), error,
                    operation='check_database_capacity.main')
        if isinstance(error, SessionNotFoundError):
            message = '会话不存在或不属于指定用户。'
        elif isinstance(error, MySQLError):
            message = ('查询超过时间限制，请缩小统计范围后重试。' if error.errno == 3024
                       else '数据库检查失败，请稍后重试并查看服务器日志。')
        elif isinstance(error, FileExistsError):
            message = '报告文件已存在，请换一个输出文件名。'
        elif isinstance(error, OSError):
            message = '无法读取 SQL 或保存报告，请检查目录和权限。'
        else:
            message = '检查参数或服务器配置不正确，请核对说明并查看日志。'
        print(message, file=sys.stderr)
        return 1
    print(markdown)
    print('报告已保存：', target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
