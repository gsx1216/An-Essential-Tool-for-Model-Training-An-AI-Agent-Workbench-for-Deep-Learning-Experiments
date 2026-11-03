"""主网络选择是分析配置，不改写训练启动快照。"""
from pathlib import Path
from app.database import get_connection
from app.errors import RunNotFoundError
from app.logging_config import logged_operation

class RunNetworkRepository:
    def __init__(self,password):
        self.password=password
        root=Path(__file__).resolve().parents[2]/'sql'
        self.get_sql=(root/'get_run_network_source.sql').read_text(encoding='utf-8')
        self.save_sql=(root/'save_run_network_source.sql').read_text(encoding='utf-8')
        self.owner_sql=(root/'event_owned_run.sql').read_text(encoding='utf-8')

    @logged_operation('run_network_repository.get')
    def get(self,user_id,run_id):
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.owner_sql,(user_id,run_id))
                if cursor.fetchone() is None:raise RunNotFoundError('运行不存在或不可访问。')
                cursor.execute(self.get_sql,(user_id,run_id))
                row=cursor.fetchone()
                if row and row['selected_at']:row['selected_at']=row['selected_at'].isoformat()
                return row

    @logged_operation('run_network_repository.save')
    def save(self,user_id,run_id,path,sha):
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(self.owner_sql.rstrip().rstrip(';')+' FOR UPDATE',(user_id,run_id))
                    if cursor.fetchone() is None:raise RunNotFoundError('运行不存在或不可访问。')
                    cursor.execute(self.save_sql,(path,sha,user_id,run_id))
                connection.commit()
            except Exception:connection.rollback();raise
        return self.get(user_id,run_id)
