"""事务 Outbox 与分析任务 Inbox；数据库是用户归属及报告快照的唯一事实来源。"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.database import get_connection
from app.errors import RunNotFoundError
from app.logging_config import logged_operation
from app.models.event_bus import EventBusyError,EventNotFoundError,EventPayloadError
from app.models.experiment_run import normalize_run_uuid
from app.repositories.experiment_repository import _utc
from app.validators import validate_positive_integer


def report_event_id(run_id,version):
    # 用稳定标识去重，不作为认证凭证。
    return hashlib.sha256(f'training.report_ready:{run_id}:{version}'.encode()).hexdigest()[:32]


def decode_event(row):
    if row is None:raise EventNotFoundError('事件不存在。')
    result=dict(row)
    try:
        text=result['report_json']
        if len(text.encode())>131072 or hashlib.sha256(text.encode()).hexdigest()!=result['report_digest']:
            raise ValueError('digest mismatch')
        data=json.loads(text)
        if (type(data) is not dict or result['event_type']!='training.report_ready' or type(data.get('schema_version')) is not int or data.get('schema_version')!=2 or data.get('run_id')!=result['run_id']
                or data.get('status') not in {'succeeded','failed','cancelled'}
                or data.get('collection_complete') is not True):raise ValueError('invalid snapshot')
        result['report']=data
    except (TypeError,ValueError,KeyError) as error:
        raise EventPayloadError('事件报告快照校验失败。') from error
    return result


class EventRepository:
    def __init__(self,password):
        self.password=password
        root=Path(__file__).resolve().parents[2]/'sql'
        for name in ('get_event','next_event','claim_event','settle_event','retry_event','list_run_events',
                     'list_agent_tasks','create_analysis_task','event_owned_run','receive_report_event'):
            setattr(self,name+'_sql',(root/(name+'.sql')).read_text(encoding='utf-8'))

    def _owner(self,cursor,user_id,run_id):
        cursor.execute(self.event_owned_run_sql,(user_id,run_id))
        if cursor.fetchone() is None:raise RunNotFoundError('运行不存在或不可访问。')

    @logged_operation('event_repository.claim',log_success=False)
    def claim(self,lease_seconds):
        if type(lease_seconds) is not int or not 10<=lease_seconds<=300:raise ValueError('事件租约不符合要求。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.next_event_sql);row=cursor.fetchone()
                    if row is None:connection.rollback();return None
                    token=uuid4().hex
                    cursor.execute(self.claim_event_sql,(token,lease_seconds,row['id']))
                    cursor.execute(self.get_event_sql,(row['id'],));result=cursor.fetchone()
                connection.commit();return result
            except Exception:connection.rollback();raise

    @logged_operation('event_repository.settle',log_success=False)
    def settle(self,event_id,token,*,status,failure_count,delay,error_code=None,redis_id=None):
        event_id=normalize_run_uuid(event_id);token=normalize_run_uuid(token)
        if (status not in {'pending','sent','attention'} or type(failure_count) is not int or not 0<=failure_count<=50
                or type(delay) is not int or not 0<=delay<=3600
                or error_code not in {None,'REDIS_UNAVAILABLE','INTERNAL_ERROR','PAYLOAD_INVALID'}):
            raise ValueError('事件投递结果不符合要求。')
        if redis_id is not None:
            import re
            if not isinstance(redis_id,str) or not re.fullmatch(r'[0-9]{1,24}-[0-9]{1,24}',redis_id):raise ValueError('Redis 消息编号错误。')
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.settle_event_sql,(status,failure_count,error_code,delay,redis_id,status,event_id,token))
                saved=cursor.rowcount==1
            connection.commit();return saved

    @logged_operation('event_repository.receive')
    def receive(self,event_id):
        event_id=normalize_run_uuid(event_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    # 与报告生成一致：先锁 run，再锁 event，避免外键与重复入队互相等待。
                    cursor.execute(self.get_event_sql,(event_id,));peek=cursor.fetchone()
                    if peek is None:raise EventNotFoundError('事件不存在。')
                    cursor.execute(self.event_owned_run_sql.rstrip().rstrip(';')+' FOR UPDATE',(peek['user_id'],peek['run_id']))
                    if cursor.fetchone() is None:raise RunNotFoundError('运行不存在或不可访问。')
                    cursor.execute(self.get_event_sql.rstrip().rstrip(';')+' FOR UPDATE',(event_id,))
                    row=decode_event(cursor.fetchone())
                    cursor.execute(self.create_analysis_task_sql,(uuid4().hex,row['user_id'],row['run_id'],row['id'],row['report_version']))
                    created=cursor.rowcount==1
                    cursor.execute(self.receive_report_event_sql,(event_id,))
                connection.commit();return created
            except Exception:connection.rollback();raise

    @logged_operation('event_repository.list')
    def list(self,user_id,run_id,*,tasks=False,limit=20,offset=0):
        user_id=validate_positive_integer(user_id,'用户 ID');run_id=normalize_run_uuid(run_id)
        if type(limit) is not int or not 1<=limit<=50 or type(offset) is not int or not 0<=offset<=10000:
            raise ValueError('分页不符合要求。')
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._owner(cursor,user_id,run_id)
                cursor.execute(self.list_agent_tasks_sql if tasks else self.list_run_events_sql,(user_id,run_id,limit,offset))
                rows=cursor.fetchall()
                for row in rows:
                    for key in ('created_at','next_attempt_at','sent_at'):
                        if row.get(key) is not None:row[key]=_utc(row[key])
                    if 'lease_active' in row:row['lease_active']=bool(row['lease_active'])
                return rows

    @logged_operation('event_repository.retry')
    def retry(self,user_id,run_id,event_id):
        user_id=validate_positive_integer(user_id,'用户 ID');run_id=normalize_run_uuid(run_id);event_id=normalize_run_uuid(event_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    self._owner(cursor,user_id,run_id)
                    cursor.execute(self.get_event_sql.rstrip().rstrip(';')+' FOR UPDATE',(event_id,))
                    row=cursor.fetchone()
                    if row is None or row['user_id']!=user_id or row['run_id']!=run_id:raise EventNotFoundError('事件不存在或不可访问。')
                    if row['lease_active']:raise EventBusyError('事件正在投递，请稍后重试。')
                    cursor.execute(self.retry_event_sql,(event_id,user_id,run_id))
                connection.commit()
            except Exception:connection.rollback();raise
