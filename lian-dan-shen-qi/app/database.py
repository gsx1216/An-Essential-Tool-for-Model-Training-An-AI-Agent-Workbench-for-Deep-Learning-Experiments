"""数据库基础设施：建立连接、执行受控建表迁移；业务仓储负责各自的提交、回滚和身份数据查询。"""

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import mysql.connector

from app.config import get_mysql_password
from app.logging_config import log_failure, logged_operation
from app.validators import validate_positive_integer


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_connection(password: str):
    return mysql.connector.connect(
        host="127.0.0.1",
        port=3306,
        user="root",
        password=password,
        charset="utf8mb4",
        connection_timeout=10,
    )


def _read_sql(filename: str) -> str:
    return (PROJECT_ROOT / "sql" / filename).read_text(encoding="utf-8")


def _backup_orphan_owners(rows: list) -> Path:
    backup_directory = PROJECT_ROOT / "data" / "migration_backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / f"session_owners_{uuid4().hex}.json"
    data = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "reason": "user_id 指向不存在的用户；原始归属备份",
        "sessions": [
            {"session_id": row[0], "old_user_id": row[1], "title": row[2]}
            for row in rows
        ],
    }
    # 先成功保存原始归属，随后才能修改数据库；备份失败则不修改。
    with backup_path.open("x", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    return backup_path


def _ensure_session_user_fk(connection, repair_orphan_owners: bool = False) -> Path | None:
    backup_path = None
    with connection.cursor() as cursor:
        cursor.execute(_read_sql("session_user_fk_exists.sql"))
        if cursor.fetchone() is not None:
            return None

        cursor.execute(_read_sql("find_orphan_sessions.sql"))
        orphan_sessions = cursor.fetchall()
        if orphan_sessions:
            if not repair_orphan_owners:
                raise ValueError(
                    f"发现 {len(orphan_sessions)} 条会话关联了不存在的用户，暂未修改旧记录。"
                    "先运行 python -m app.database --check-user 2 查看；"
                    "如要保留会话并取消无效归属，再运行 "
                    "python -m app.database --repair-orphan-owners。"
                )
            backup_path = _backup_orphan_owners(orphan_sessions)
            repair_sql = _read_sql("clear_orphan_session_owner.sql")
            for session_id, old_user_id, _title in orphan_sessions:
                # 精确限制到已备份的记录；如果该用户此刻已存在，则不清空归属。
                cursor.execute(repair_sql, (session_id, old_user_id))
            connection.commit()

        # 旧表不能靠 CREATE TABLE IF NOT EXISTS 自动更新，必须单独 ALTER TABLE。
        cursor.execute(_read_sql("add_session_user_fk.sql"))
    connection.commit()
    return backup_path


def _ensure_user_password_column(connection) -> bool:
    """增量迁移，不删除用户、不为旧用户设置默认密码。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql("user_password_column.sql"))
        column = cursor.fetchone()
        added = column is None
        if added:
            cursor.execute(_read_sql("add_user_password_hash.sql"))
        elif column[0] != "varchar" or column[1] < 255 or column[2] != "YES":
            raise ValueError("users.password_hash 应为可空 VARCHAR，长度至少为 255，请检查表结构。")
    connection.commit()
    return added


def _ensure_chat_turns(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_read_sql("create_chat_turns.sql"))
        cursor.execute(_read_sql("backfill_pending_turns.sql"))
    connection.commit()


def _ensure_skills(connection):
    """增量创建个人 Skill 表与轮次快照字段，不改历史聊天。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('create_personal_skills.sql'))
        cursor.execute(_read_sql('chat_turn_skill_column.sql'))
        if cursor.fetchone()[0] == 0:
            cursor.execute(_read_sql('add_chat_turn_skill.sql'))
    connection.commit()


def _ensure_login_sessions(connection) -> None:
    """只创建登录记录表，不修改已有用户、聊天或登录记录。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql("create_login_sessions.sql"))
    connection.commit()


def _ensure_model_credentials(connection) -> None:
    """增量创建个人密文表，不改变现有用户、会话或登录记录。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql("create_model_credentials.sql"))
    connection.commit()


def _ensure_context_summaries(connection) -> None:
    """仅增量创建摘要表，摘要随所属会话删除；不改写已有聊天原文。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql("create_context_summaries.sql"))
    connection.commit()


def _ensure_experiments(connection) -> None:
    """增量创建实验定义表；不清空聊天、用户或已有实验。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('create_experiments.sql'))
    connection.commit()


def _ensure_experiment_runs(connection) -> None:
    """补父表归属唯一键后建运行表；不清空记录，不启动任何运行。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('experiment_run_owner_index.sql'))
        rows = list(cursor.fetchall())
        if not rows:
            cursor.execute(_read_sql('add_experiment_run_owner_index.sql'))
        elif rows != [(1, 'id', 0), (2, 'user_id', 0)]:
            raise ValueError('实验归属索引结构不符合运行外键要求。')
        cursor.execute(_read_sql('create_experiment_runs.sql'))
    connection.commit()


def _ensure_server_connections(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('create_server_connections.sql'))
        cursor.execute(_read_sql('server_deleted_column.sql'))
        if cursor.fetchone()[0]==0:cursor.execute(_read_sql('add_server_deleted_column.sql'))
        cursor.execute(_read_sql('server_secret_constraint.sql'))
        row=cursor.fetchone()
        if row is None or 'deleted_at' not in row[0].lower():
            if row is not None:cursor.execute(_read_sql('drop_server_secret_constraint.sql'))
            cursor.execute(_read_sql('add_server_secret_constraint.sql'))
    connection.commit()


def _ensure_run_executions(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('runner_parent_indexes.sql'))
        rows = list(cursor.fetchall())
        for table, index, file in [('experiment_runs', 'uq_runs_identity_owner', 'add_runner_run_owner_index.sql'),
                                   ('server_connections', 'uq_servers_identity_owner', 'add_runner_server_owner_index.sql')]:
            found = [(r[2], r[3], r[4]) for r in rows if r[0] == table and r[1] == index]
            if not found:
                cursor.execute(_read_sql(file))
            elif found != [(1, 'id', 0), (2, 'user_id', 0)]:
                raise ValueError('Runner 归属父键结构不正确。')
        cursor.execute(_read_sql('create_run_executions.sql'))
    connection.commit()


def _ensure_run_monitors(connection):
    """只增量创建监控队列并登记已有非终态执行；不连接 SSH。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('create_run_monitor_tasks.sql'))
        cursor.execute(_read_sql('backfill_active_run_monitors.sql'))
    connection.commit()


def _ensure_training_tools(connection):
    """只增量创建任务目录和计划，不连接 SSH，不启动训练。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('create_training_tasks.sql'))
        cursor.execute(_read_sql('training_task_deleted_column.sql'))
        if cursor.fetchone()[0]==0:
            cursor.execute(_read_sql('add_training_task_delete_columns.sql'))
        cursor.execute(_read_sql('create_chat_training_plans.sql'))
        cursor.execute(_read_sql('create_training_launch_claims.sql'))
        # 训练工具与研究工具共用六次预算；独立增量迁移也要升级旧的四次约束。
        import re
        cursor.execute(_read_sql('research_call_limit.sql'))
        row=cursor.fetchone()
        clause=re.sub(r'[`()\s]','',str(row[0]).lower()) if row else ''
        if clause=='call_indexbetween1and4':cursor.execute(_read_sql('extend_research_call_limit.sql'))
        elif clause!='call_indexbetween1and6':raise ValueError('聊天工具次数约束不符合训练工具要求。')
    connection.commit()


def _ensure_metrics(connection) -> None:
    with connection.cursor() as cursor:
        for name in ('create_run_metric_streams.sql', 'create_run_metric_events.sql', 'create_run_reports.sql','create_run_metric_parsers.sql','create_run_network_sources.sql'):
            cursor.execute(_read_sql(name))
    connection.commit()


def _ensure_event_bus(connection):
    """报告 Outbox 与分析 Inbox；不连接 Redis，不调用模型或自动回填旧报告。"""
    with connection.cursor() as cursor:
        for name in ('create_event_outbox.sql','create_agent_tasks.sql'):cursor.execute(_read_sql(name))
    connection.commit()


def _ensure_analysis(connection):
    """增量扩充 Inbox 租约并新增偏好/结果表；默认关闭，不调用模型或回填结果。"""
    _ensure_event_bus(connection)
    expected = {
        "attempts": ("bigint unsigned", "NO"), "attempts_in_cycle": ("int unsigned", "NO"),
        "lease_token": ("char(32)", "YES"), "lease_until": ("datetime(6)", "YES"),
        "next_attempt_at": ("datetime(6)", "YES"), "last_error_code": ("varchar(48)", "YES"),
        "started_at": ("datetime(6)", "YES"), "finished_at": ("datetime(6)", "YES"),
    }
    with connection.cursor() as cursor:
        cursor.execute(_read_sql("analysis_task_columns.sql"))
        rows = cursor.fetchall()
        if not rows:
            cursor.execute(_read_sql("add_analysis_task_columns.sql"))
            cursor.execute(_read_sql("analysis_task_columns.sql"))
            rows = cursor.fetchall()
        found = {row[0]: (row[1].lower(), row[2]) for row in rows}
        if found != expected:
            raise ValueError("分析任务字段存在部分迁移或类型不一致，未覆盖旧数据，请检查结构。")
        for name in ("create_analysis_preferences.sql", "create_analysis_results.sql"):
            cursor.execute(_read_sql(name))
    connection.commit()
    _ensure_suggestions(connection)


def _ensure_evaluation(connection):
    """DDL 可重复执行；不猜测旧数据集，不改写游标或旧报告。"""
    with connection.cursor() as cursor:
        for name in ('create_experiment_evaluation_specs.sql', 'create_run_evaluation_specs.sql'):
            cursor.execute(_read_sql(name))
        cursor.execute(_read_sql('metric_dataset_column.sql'))
        column = cursor.fetchone()
        if column is None:
            cursor.execute(_read_sql('add_metric_dataset.sql'))
        elif tuple(column) != ('varchar(64)', 'ascii', 'ascii_bin', 'NO', 'unspecified'):
            raise ValueError('指标数据集字段结构不符合要求。')
        cursor.execute(_read_sql('metric_dataset_index.sql'))
        index = list(cursor.fetchall())
        if not index:
            cursor.execute(_read_sql('add_metric_dataset_index.sql'))
        elif index != [(1, 'run_id', 1), (2, 'dataset', 1), (3, 'split', 1), (4, 'source_offset', 1)]:
            raise ValueError('指标数据集索引结构不符合要求。')
    connection.commit()


def _ensure_suggestions(connection):
    """扩展既有任务类型并补建议偏好/结果/证据；旧分析只登记队列，不调用模型。"""
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('suggestion_agent_check.sql'))
        row = cursor.fetchone()
        clause = str(row[0]).lower().replace("\\'", "'") if row else ''
        clause = re.sub(r'_(?:utf8mb4|utf8mb3|ascii)(?=\x27)', '', clause)
        clause = re.sub(r'[`()\s]', '', clause)
        if clause == "agent_name='analysis'":
            cursor.execute(_read_sql('extend_suggestion_agent.sql'))
        elif clause != "agent_namein'analysis','suggestion'":
            raise ValueError('Agent 类型约束不符合建议队列要求。')
        for name in ('create_suggestion_preferences', 'create_suggestion_results', 'create_suggestion_evidence', 'backfill_suggestion_tasks'):
            cursor.execute(_read_sql(name + '.sql'))
    connection.commit()


def _ensure_papers(connection):
    import re
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('paper_fulltext_session.sql'))
        for name in ('create_papers.sql','create_paper_files.sql','create_paper_chunks.sql'):
            cursor.execute(_read_sql(name))
        for query,old,new,alter in (
            ('paper_chunk_limit.sql','char_lengthcontent_textbetween1and1000','char_lengthcontent_textbetween1and6000','extend_paper_chunk_limit.sql'),
            ('paper_tool_meta_limit.sql','lengthresult_meta_json<=4096','lengthresult_meta_json<=24576','extend_paper_tool_meta_limit.sql'),
        ):
            cursor.execute(_read_sql(query));row=cursor.fetchone()
            if row is None and query=='paper_tool_meta_limit.sql':continue
            clause=re.sub(r'[`()\s]','',str(row[0]).lower()) if row else ''
            if old in clause:cursor.execute(_read_sql(alter))
            elif new not in clause:raise ValueError('论文块或工具审计约束不符合增量迁移要求。')
    connection.commit()


def _ensure_revision_runs(connection):
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('revision_change_owner_index.sql'))
        if cursor.fetchone()[0]==0:cursor.execute(_read_sql('add_revision_change_owner_index.sql'))
        for name in ('create_revision_runs.sql','create_revision_launch_claims.sql'):
            cursor.execute(_read_sql(name))
    connection.commit()


def _ensure_code_changes(connection):
    with connection.cursor() as cursor:
        for name in ('create_code_changes.sql', 'create_code_change_claims.sql'):
            cursor.execute(_read_sql(name))
    connection.commit()


def _ensure_chat_tools(connection):
    with connection.cursor() as cursor:
        cursor.execute(_read_sql('chat_tool_session_index.sql'))
        rows = list(cursor.fetchall())
        if not rows:
            cursor.execute(_read_sql('add_chat_tool_session_index.sql'))
        elif rows != [(1, 'id', 0), (2, 'user_id', 0)]:
            raise ValueError('聊天归属索引结构不符合工具外键要求。')
        cursor.execute(_read_sql('chat_tool_turn_index.sql'))
        rows = list(cursor.fetchall())
        if not rows:
            cursor.execute(_read_sql('add_chat_tool_turn_index.sql'))
        elif rows != [(1, 'session_id', 0), (2, 'id', 0)]:
            raise ValueError('聊天轮次索引结构不符合工具外键要求。')
        for name in ('create_chat_run_bindings.sql', 'create_chat_tool_calls.sql'):
            cursor.execute(_read_sql(name))
    connection.commit()


def _ensure_research(connection):
    """只补来源/建议/限速表，将工具审计上限由四次扩到六次；保留原记录。"""
    import re
    with connection.cursor() as cursor:
        for name in ('create_research_references.sql','create_research_advice.sql','create_research_rate_limits.sql'):
            cursor.execute(_read_sql(name))
        cursor.execute(_read_sql('research_call_limit.sql'))
        row=cursor.fetchone()
        clause=re.sub(r'[`()\s]','',str(row[0]).lower()) if row else ''
        if clause=='call_indexbetween1and4':cursor.execute(_read_sql('extend_research_call_limit.sql'))
        elif clause!='call_indexbetween1and6':raise ValueError('聊天工具次数约束不符合增量迁移要求。')
    connection.commit()


def initialize_database(password: str, repair_orphan_owners: bool = False) -> Path | None:
    with get_connection(password) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(_read_sql("init.sql"))
                while cursor.nextset():
                    pass
            _ensure_user_password_column(connection)
            backup_path = _ensure_session_user_fk(connection, repair_orphan_owners)
            _ensure_chat_turns(connection)
            _ensure_login_sessions(connection)
            _ensure_model_credentials(connection)
            _ensure_context_summaries(connection)
            _ensure_experiments(connection)
            _ensure_experiment_runs(connection)
            _ensure_server_connections(connection)
            _ensure_run_executions(connection)
            _ensure_run_monitors(connection)
            _ensure_metrics(connection)
            _ensure_event_bus(connection)
            _ensure_analysis(connection)
            _ensure_evaluation(connection)
            _ensure_chat_tools(connection)
            _ensure_research(connection)
            _ensure_training_tools(connection)
            _ensure_code_changes(connection)
            _ensure_revision_runs(connection)
            _ensure_papers(connection)
            _ensure_skills(connection)
            return backup_path
        except Exception:
            connection.rollback()
            raise


def check_user_and_orphans(password: str, user_id: int) -> tuple[tuple | None, list]:
    """只读检查：不创建用户、不修改会话，也不执行建表语句。"""
    user_id = validate_positive_integer(user_id, "用户 ID")
    with get_connection(password) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_read_sql("get_user.sql"), (user_id,))
            user = cursor.fetchone()
            cursor.execute(_read_sql("find_orphan_sessions.sql"))
            return user, cursor.fetchall()


@logged_operation("database.main", log_success=False)
def main():
    parser = argparse.ArgumentParser(description="初始化数据库、补充外键或只读检查用户。")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check-user", type=int, metavar="USER_ID", help="只读查询用户和无效归属的会话")
    actions.add_argument(
        "--user-password-schema", action="store_true",
        help="仅为已有 users 表补密码哈希字段，不修改用户和会话记录",
    )
    actions.add_argument(
        "--login-session-schema", action="store_true",
        help="仅为已有项目库创建登录记录表，不修改用户和聊天记录",
    )
    actions.add_argument(
        "--model-credential-schema", action="store_true",
        help="仅为已有项目库创建个人模型密文表，不修改现有记录",
    )
    actions.add_argument(
        "--context-summary-schema", action="store_true",
        help="仅为已有项目库创建上下文摘要表，不修改聊天原文",
    )
    actions.add_argument(
        "--repair-orphan-owners",
        action="store_true",
        help="先备份无效归属，再将其设为 NULL，保留会话和消息并补加外键",
    )
    actions.add_argument('--experiment-schema', action='store_true',
                        help='仅增量创建实验定义表，不改动已有用户、聊天或实验记录')
    actions.add_argument('--experiment-run-schema', action='store_true',
                        help='补实验归属唯一索引及运行表，不运行聊天回填或训练')
    actions.add_argument('--server-schema', action='store_true', help='仅补服务器配置表，不连接 SSH 或修改已有记录')
    actions.add_argument('--runner-schema', action='store_true', help='仅补运行执行绑定表，不启动训练或回填旧记录')
    actions.add_argument('--metrics-schema', action='store_true', help='仅补指标游标、事件和报告表，不启动训练或回填旧记录')
    actions.add_argument('--evaluation-schema', action='store_true', help='增量创建评测声明、运行快照和数据集维度')
    actions.add_argument('--chat-tools-schema', action='store_true', help='增量创建会话运行关联与只读工具审计表')
    actions.add_argument('--research-schema',action='store_true',help='增量创建公开资料、建议与限速表，不调用模型或训练')
    actions.add_argument('--training-tools-schema',action='store_true',help='增量创建训练目录和对话计划，不启动训练')
    actions.add_argument('--monitor-schema',action='store_true',help='增量创建监控任务队列，不连接 SSH 或启动训练')
    actions.add_argument('--event-bus-schema',action='store_true',help='增量创建可靠事件与分析任务表，不连接 Redis 或调用模型')
    actions.add_argument('--analysis-schema',action='store_true',help='增量创建分析租约、本人开关和结果表；不开启模型调用')
    actions.add_argument('--suggestion-schema',action='store_true',help='增量创建建议开关、结果和证据并登记已有分析；不调用模型或训练')
    actions.add_argument('--code-change-schema',action='store_true',help='增量创建修改记录与聊天领取表；不改远端代码或调用模型')
    actions.add_argument('--revision-schema',action='store_true',help='增量创建修改后运行绑定和聊天启动领取；不训练')
    actions.add_argument('--paper-schema',action='store_true',help='创建个人论文原件、元信息、分块全文索引；不解析 PDF 或调用模型')
    actions.add_argument('--skill-schema',action='store_true',help='增量增加聊天轮次Skill快照；不读取模型Key或执行任务')
    args = parser.parse_args()
    try:
        password = get_mysql_password()
        if args.user_password_schema:
            with get_connection(password) as connection:
                _ensure_user_password_column(connection)
            print("用户密码哈希字段已确认，已有记录与密码均未修改。")
        elif args.login_session_schema:
            with get_connection(password) as connection:
                _ensure_login_sessions(connection)
            print("登录记录表已确认，已有用户和聊天记录均未修改。")
        elif args.model_credential_schema:
            with get_connection(password) as connection:
                _ensure_model_credentials(connection)
            print("个人模型密文表已确认，现有记录均未修改。")
        elif args.context_summary_schema:
            with get_connection(password) as connection:
                _ensure_context_summaries(connection)
            print("上下文摘要表已确认，现有用户、聊天原文和摘要均未修改。")
        elif args.experiment_schema:
            with get_connection(password) as connection:
                _ensure_experiments(connection)
            print('实验定义表已确认，现有记录未修改。')
        elif args.experiment_run_schema:
            with get_connection(password) as connection:
                _ensure_experiments(connection)
                _ensure_experiment_runs(connection)
            print('实验归属索引和运行表已确认，现有业务记录未修改，未启动训练。')
        elif args.skill_schema:
            with get_connection(password) as connection:
                _ensure_skills(connection)
            print('个人Skill表和聊天轮次Skill快照字段已确认；未改变历史消息、调用模型或启动训练。')
        elif args.paper_schema:
            with get_connection(password) as connection:
                _ensure_papers(connection)
            print('个人论文原件、元信息、分块及全文索引已确认；未调用模型或训练。')
        elif args.revision_schema:
            with get_connection(password) as connection:
                _ensure_analysis(connection)
                _ensure_code_changes(connection)
                _ensure_revision_runs(connection)
            print('修改后运行绑定与聊天领取表已确认；未调用模型、SSH 或训练。')
        elif args.code_change_schema:
            with get_connection(password) as connection:
                _ensure_analysis(connection)
                _ensure_code_changes(connection)
            print('代码修改记录与聊天领取表已确认；未调用模型、SSH 或训练。')
        elif args.suggestion_schema:
            with get_connection(password) as connection:
                _ensure_analysis(connection)
            print('建议阶段结构已确认，历史分析已登记待处理任务；默认关闭，未调用模型、SSH 或训练。')
        elif args.analysis_schema:
            with get_connection(password) as connection:_ensure_analysis(connection)
            print('分析任务租约、本人偏好和结果表已确认；未启用分析、调用模型或启动训练。')
        elif args.event_bus_schema:
            with get_connection(password) as connection:_ensure_event_bus(connection)
            print('事件 Outbox 与分析任务 Inbox 已确认，未连接 Redis、调用模型或启动训练。')
        elif args.monitor_schema:
            with get_connection(password) as connection:
                _ensure_run_monitors(connection)
            print('后台监控队列已确认，已有非终态运行已登记；未连接 SSH 或启动训练。')
        elif args.training_tools_schema:
            with get_connection(password) as connection:
                _ensure_chat_tools(connection)
                _ensure_training_tools(connection)
            print('训练目录和对话计划表已确认，未启动任何训练。')
        elif args.research_schema:
            with get_connection(password) as connection:
                _ensure_chat_tools(connection)
                _ensure_research(connection)
            print('研究资料与建议表已确认，已有记录均未清空。')
        elif args.chat_tools_schema:
            with get_connection(password) as connection:
                _ensure_chat_tools(connection)
            print('聊天运行关联和工具审计表已确认，未改写已有记录。')
        elif args.evaluation_schema:
            with get_connection(password) as connection:
                _ensure_evaluation(connection)
            print('评测声明、运行快照和指标数据集字段已确认，未改写业务记录。')
        elif args.metrics_schema:
            with get_connection(password) as connection:
                _ensure_metrics(connection)
            print('指标游标、事件和报告表已确认，现有记录未修改，未启动训练。')
        elif args.runner_schema:
            with get_connection(password) as connection:
                _ensure_run_executions(connection)
            print('运行执行绑定表已确认，现有记录未修改，未启动训练。')
        elif args.server_schema:
            with get_connection(password) as connection:
                _ensure_server_connections(connection)
            print('服务器配置表已确认，现有记录未修改，未连接 SSH。')
        elif args.check_user is not None:
            user, orphan_sessions = check_user_and_orphans(password, args.check_user)
            if user is None:
                print(f"用户 ID {args.check_user} 不存在。")
            else:
                print(f"用户存在：ID={user[0]}，用户名={user[1]}")
            print(f"关联不存在用户的会话数量：{len(orphan_sessions)}")
            for session_id, old_user_id, _title in orphan_sessions[:20]:
                print(f"  会话 {session_id} -> 不存在的用户 ID {old_user_id}")
        else:
            backup_path = initialize_database(password, args.repair_orphan_owners)
            print("数据库初始化完成，用户与会话外键及可恢复对话轮次表已确认。")
            if backup_path is not None:
                print("已保留会话和消息，并将无效用户归属设为 NULL。")
                print("原始归属备份：", backup_path)
    except (ValueError, RuntimeError, OSError, mysql.connector.Error) as error:
        log_failure(logging.getLogger("app.database"), error)
        print("操作失败：", error)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
