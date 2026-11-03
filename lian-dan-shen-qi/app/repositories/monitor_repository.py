"""MySQL 持久化监控队列；行锁领取、租约恢复、令牌条件写回。"""
from pathlib import Path
from uuid import uuid4

from app.database import get_connection
from app.logging_config import logged_operation
from app.models.experiment_run import normalize_run_uuid
from app.models.monitor import ERROR_CODES,MonitorBusyError,MonitorNotFoundError
from app.repositories.experiment_repository import _utc
from app.validators import validate_positive_integer


def identity(user_id,run_id):
    return validate_positive_integer(user_id,'用户 ID'),normalize_run_uuid(run_id)


class MonitorRepository:
    def __init__(self,password):
        self.password=password
        root=Path(__file__).resolve().parents[2]/'sql'
        for name in ('enqueue_run_monitor','get_run_monitor','next_run_monitor','claim_run_monitor',
                     'renew_run_monitor','settle_run_monitor','retry_run_monitor','backfill_active_run_monitors'):
            setattr(self,name+'_sql',(root/(name+'.sql')).read_text(encoding='utf-8'))

    @staticmethod
    def decode(row):
        if row is None:raise MonitorNotFoundError('此运行尚未登记后台监控。')
        result=dict(row);result['lease_active']=bool(result['lease_active'])
        for key in ('next_poll_at','last_attempt_at','last_success_at','created_at','updated_at','completed_at','lease_until'):
            if result[key] is not None:result[key]=_utc(result[key])
        return result

    def _read(self,cursor,user_id,run_id,lock=False):
        text=self.get_run_monitor_sql.rstrip().rstrip(';')+(' FOR UPDATE' if lock else '')
        cursor.execute(text,(user_id,run_id))
        return self.decode(cursor.fetchone())

    @logged_operation('monitor_repository.reconcile',log_success=False)
    def reconcile(self):
        """有界补偿旧版本/部署间隙的活跃绑定；不改变已有任务状态。"""
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:cursor.execute(self.backfill_active_run_monitors_sql)
            connection.commit()

    @logged_operation('monitor_repository.get')
    def get(self,user_id,run_id):
        user_id,run_id=identity(user_id,run_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:return self._read(cursor,user_id,run_id)

    @logged_operation('monitor_repository.claim',log_success=False)
    def claim(self,lease_seconds):
        if type(lease_seconds) is not int or not 30<=lease_seconds<=600:raise ValueError('租约不符合要求。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.next_run_monitor_sql);row=cursor.fetchone()
                    if row is None:connection.rollback();return None
                    cursor.execute(self.claim_run_monitor_sql,(uuid4().hex,lease_seconds,row['user_id'],row['run_id']))
                    result=self._read(cursor,row['user_id'],row['run_id'])
                connection.commit();return result
            except Exception:connection.rollback();raise

    @logged_operation('monitor_repository.renew',log_success=False)
    def renew(self,user_id,run_id,token,lease_seconds):
        user_id,run_id=identity(user_id,run_id);token=normalize_run_uuid(token,'监控租约')
        if type(lease_seconds) is not int or not 30<=lease_seconds<=600:raise ValueError('租约不符合要求。')
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.renew_run_monitor_sql,(lease_seconds,user_id,run_id,token));saved=cursor.rowcount==1
            connection.commit();return saved

    @logged_operation('monitor_repository.settle')
    def settle(self,user_id,run_id,token,*,status,delay,failure_count=0,error_code=None,report_version=None,success=False):
        user_id,run_id=identity(user_id,run_id);token=normalize_run_uuid(token,'监控租约')
        if (status not in {'scheduled','completed','attention'} or type(delay) is not int or not 0<=delay<=3600
            or type(failure_count) is not int or not 0<=failure_count<=50 or error_code not in ERROR_CODES or type(success) is not bool):
            raise ValueError('监控结果不符合要求。')
        if report_version is not None:validate_positive_integer(report_version,'报告版本')
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.settle_run_monitor_sql,(status,delay,failure_count,error_code,report_version,success,status,user_id,run_id,token))
                saved=cursor.rowcount==1
            connection.commit();return saved

    @logged_operation('monitor_repository.retry')
    def retry(self,user_id,run_id):
        user_id,run_id=identity(user_id,run_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    # SELECT 来源固定到本人执行记录；服务还会先校验完整 run 归属。
                    cursor.execute(self.enqueue_run_monitor_sql,(user_id,run_id))
                    record=self._read(cursor,user_id,run_id,lock=True)
                    if record['status']=='running' and record['lease_active']:
                        raise MonitorBusyError('监控任务正在处理，请稍后重试。')
                    cursor.execute(self.retry_run_monitor_sql,(user_id,run_id))
                    result=self._read(cursor,user_id,run_id)
                connection.commit();return result
            except Exception:connection.rollback();raise
