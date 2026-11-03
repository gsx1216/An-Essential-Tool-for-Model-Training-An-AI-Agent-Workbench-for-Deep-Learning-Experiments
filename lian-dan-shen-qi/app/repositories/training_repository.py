"""训练目录与持久化计划；短事务锁会话，SSH 等待期间不持数据库锁。"""
import json
from pathlib import Path
from uuid import uuid4

from mysql.connector import IntegrityError,errorcode

from app.database import get_connection
from app.logging_config import logged_operation
from app.models.training_task import (TrainingConflictError,TrainingNotFoundError,canonical,digest,require_start_instruction)
from app.models.experiment_run import normalize_run_uuid
from app.repositories.context_repository import ContextRepository
from app.validators import validate_positive_integer


def plan_record(row):
    if row is None:raise TrainingNotFoundError('训练计划不存在或不可访问。')
    result=dict(row);result['snapshot']=json.loads(result.pop('snapshot_json'))
    if digest(result['snapshot'])!=result['snapshot_sha256']:
        raise RuntimeError('训练计划摘要校验失败。')
    return result


class TrainingRepository(ContextRepository):
    def __init__(self,password):
        super().__init__(password)
        root=Path(__file__).resolve().parents[2]/'sql'
        for name in ('create_training_task','delete_training_task','get_training_task','get_training_task_by_script','list_training_tasks','get_training_question',
                     'create_chat_training_plan','get_chat_training_plan','get_chat_training_plan_turn',
                     'latest_chat_training_plan','claim_chat_training_plan','bind_training_plan_run',
                     'get_training_launch_claim','create_training_launch_claim','get_revision_launch_claim'):
            setattr(self,name+'_sql',(root/(name+'.sql')).read_text(encoding='utf-8'))

    @logged_operation('training_repository.create_task')
    def create_task(self,user_id,name,experiment_id,server_id,script_id,configuration):
        user_id=validate_positive_integer(user_id,'用户 ID');task_id=uuid4().hex
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(self.create_training_task_sql,(task_id,user_id,name,experiment_id,server_id,script_id,canonical(configuration),server_id,user_id))
                    if cursor.rowcount!=1:raise TrainingNotFoundError('服务器不存在或已删除。')
                connection.commit()
            except IntegrityError as error:
                connection.rollback()
                if error.errno==errorcode.ER_DUP_ENTRY:raise TrainingConflictError('当前用户已存在这个任务名称。') from error
                if error.errno==errorcode.ER_NO_REFERENCED_ROW_2:raise TrainingNotFoundError('实验或服务器不存在或不可访问。') from error
                raise
            except Exception:
                connection.rollback();raise
        return self.get_task(user_id,task_id)

    @logged_operation('training_repository.get_task')
    def get_task(self,user_id,task_id):
        user_id=validate_positive_integer(user_id,'用户 ID');task_id=normalize_run_uuid(task_id,'任务 ID')
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_training_task_sql,(user_id,task_id));row=cursor.fetchone()
        if row is None:raise TrainingNotFoundError('训练任务不存在或不可访问。')
        row['configuration']=json.loads(row.pop('configuration_json'))
        return row

    @logged_operation('training_repository.delete_task')
    def delete_task(self,user_id,task_id):
        user_id=validate_positive_integer(user_id,'用户 ID')
        task_id=normalize_run_uuid(task_id,'任务 ID')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(self.delete_training_task_sql,(user_id,task_id))
                    if cursor.rowcount!=1:
                        raise TrainingNotFoundError('训练任务不存在或不可访问。')
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _require_active_task(self,cursor,user_id,task_id):
        # 与删除更新同一行锁定，避免拿着删除前读到的配置创建/领取新计划。
        cursor.execute(self.get_training_task_sql.rstrip().rstrip(';')+' FOR UPDATE',(user_id,task_id))
        if cursor.fetchone() is None:
            raise TrainingNotFoundError('训练任务不存在或已删除。')

    @logged_operation('training_repository.get_by_script')
    def get_by_script(self, user_id, script_id):
        from app.models.runner import script_identifier
        user_id = validate_positive_integer(user_id, '用户 ID')
        script_id = script_identifier(script_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_training_task_by_script_sql, (user_id, script_id))
                row = cursor.fetchone()
        if row is None:
            raise TrainingNotFoundError('训练入口不存在或不属于当前用户。')
        row['configuration'] = json.loads(row.pop('configuration_json'))
        return row

    @logged_operation('training_repository.list_tasks')
    def list_tasks(self,user_id,query='',offset=0,limit=5):
        from app.models.training_task import ListTasks
        args=ListTasks(query=query,offset=offset,limit=limit);user_id=validate_positive_integer(user_id,'用户 ID')
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.list_training_tasks_sql,(user_id,args.query,args.limit+1,args.offset));rows=cursor.fetchall()
        items=rows[:args.limit]
        return {'items':items,'offset':offset,'next_offset':offset+len(items) if len(rows)>limit else None}

    def _locked(self,cursor,user_id,session_id,attempt):
        from app.models.context_snapshot import ContextAttempt
        if not isinstance(attempt,ContextAttempt):raise ValueError('缺少当前生成尝试。')
        self._require_session(cursor,user_id,session_id)
        self._require_attempt(cursor,session_id,attempt)

    @logged_operation('training_repository.question')
    def question(self,user_id,session_id,attempt):
        user_id,session_id=self._identity(user_id,session_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._locked(cursor,user_id,session_id,attempt)
                    cursor.execute(self.get_training_question_sql,(session_id,attempt.turn_id));row=cursor.fetchone()
                    if row is None:raise TrainingNotFoundError('当前用户提问不存在。')
                connection.commit();return row[0]
            except Exception:connection.rollback();raise

    @staticmethod
    def _identity(user_id,session_id):
        from app.validators import normalize_session_id
        return validate_positive_integer(user_id,'用户 ID'),normalize_session_id(session_id)

    @logged_operation('training_repository.prepare')
    def prepare(self,user_id,session_id,attempt,snapshot):
        user_id,session_id=self._identity(user_id,session_id)
        text=canonical(snapshot);sha=digest(snapshot)
        if len(text.encode())>32768:raise ValueError('训练计划超过预算。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._locked(cursor,user_id,session_id,attempt)
                with connection.cursor(dictionary=True) as cursor:
                    self._require_active_task(cursor,user_id,snapshot['task_id'])
                    cursor.execute(self.get_chat_training_plan_turn_sql,(user_id,session_id,attempt.turn_id));row=cursor.fetchone()
                    if row:
                        result=plan_record(row)
                        if result['snapshot_sha256']!=sha:raise TrainingConflictError('本轮已有另一份计划，请用新的提问更改目标或参数。')
                    else:
                        identifier=uuid4().hex
                        cursor.execute(self.create_chat_training_plan_sql,(identifier,user_id,session_id,attempt.turn_id,
                            snapshot['task_id'],text,sha,uuid4().hex))
                        cursor.execute(self.get_chat_training_plan_sql,(user_id,session_id,identifier));result=plan_record(cursor.fetchone())
                connection.commit();return result
            except Exception:connection.rollback();raise

    @logged_operation('training_repository.get_plan')
    def get_plan(self,user_id,session_id,plan_id=None):
        user_id,session_id=self._identity(user_id,session_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                if plan_id is None:cursor.execute(self.latest_chat_training_plan_sql,(user_id,session_id))
                else:cursor.execute(self.get_chat_training_plan_sql,(user_id,session_id,normalize_run_uuid(plan_id,'计划 ID')))
                return plan_record(cursor.fetchone())

    @logged_operation('training_repository.claim')
    def claim(self,user_id,session_id,attempt,plan_id):
        user_id,session_id=self._identity(user_id,session_id);plan_id=normalize_run_uuid(plan_id,'计划 ID')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._locked(cursor,user_id,session_id,attempt)
                    cursor.execute(self.get_training_question_sql,(session_id,attempt.turn_id));question=cursor.fetchone()
                    if question is None:raise TrainingNotFoundError('当前用户提问不存在。')
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.get_chat_training_plan_sql.rstrip().rstrip(';')+' FOR UPDATE',(user_id,session_id,plan_id))
                    plan=plan_record(cursor.fetchone());snapshot=plan['snapshot']
                    self._require_active_task(cursor,user_id,plan['task_id'])
                    require_start_instruction(question[0],snapshot['name'],snapshot['task_id'],same_turn=plan['prepared_turn_id']==attempt.turn_id)
                    cursor.execute(self.get_revision_launch_claim_sql,(user_id,session_id,attempt.turn_id))
                    if cursor.fetchone():raise TrainingConflictError('本轮已领取修改后训练。')
                    # 即便重新启动旧计划，也不能让这一轮同时启动另一计划。
                    cursor.execute(self.get_training_launch_claim_sql,(user_id,session_id,attempt.turn_id))
                    other=cursor.fetchone()
                    if other and other['plan_id']!=plan_id:raise TrainingConflictError('本轮已领取另一训练计划。')
                    if other is None:
                        cursor.execute(self.create_training_launch_claim_sql,(session_id,attempt.turn_id,user_id,plan_id))
                    if plan['launch_turn_id'] is None:
                        cursor.execute(self.claim_chat_training_plan_sql,(attempt.turn_id,user_id,session_id,plan_id))
                        plan['launch_turn_id']=attempt.turn_id
                connection.commit();return plan
            except Exception:connection.rollback();raise

    @logged_operation('training_repository.bind_run')
    def bind_run(self,user_id,session_id,plan_id,run_id):
        user_id,session_id=self._identity(user_id,session_id)
        plan_id=normalize_run_uuid(plan_id);run_id=normalize_run_uuid(run_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.get_chat_training_plan_sql.rstrip().rstrip(';')+' FOR UPDATE',(user_id,session_id,plan_id))
                    plan=plan_record(cursor.fetchone())
                    if not plan['launch_turn_id'] or plan['run_id'] not in (None,run_id):
                        raise TrainingConflictError('训练计划运行绑定冲突。')
                    if plan['run_id'] is None:
                        cursor.execute(self.bind_training_plan_run_sql,(run_id,user_id,session_id,plan_id,run_id))
                        if cursor.rowcount!=1:raise TrainingConflictError('训练计划运行绑定冲突。')
                connection.commit()
            except Exception:connection.rollback();raise
