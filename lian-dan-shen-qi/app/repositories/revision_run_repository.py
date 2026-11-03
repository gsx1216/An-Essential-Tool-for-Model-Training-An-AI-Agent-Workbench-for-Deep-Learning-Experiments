"""原运行、修改记录、新运行原子关联；原始参数和评测从原运行复制。"""
from mysql.connector import IntegrityError, errorcode
from pathlib import Path
from uuid import uuid4

from app.database import get_connection
from app.errors import RunNotFoundError, RunRequestConflictError, RunStorageError
from app.logging_config import logged_operation
from app.models.analysis import compact, strict_json
from app.models.experiment_run import canonical_parameters, normalize_run_uuid, run_input_digest
from app.models.research import content_hash
from app.models.revision_run import RevisionConflict, require_revision_start
from app.repositories.code_change_repository import CodeChangeRepository
from app.repositories.experiment_run_repository import ExperimentRunRepository, _read_row
from app.repositories.experiment_repository import _utc
from app.validators import validate_positive_integer


class RevisionRunRepository(CodeChangeRepository):
    def __init__(self, password):
        super().__init__(password)
        root = Path(__file__).resolve().parents[2]/'sql'
        for name in ('get_revision_run','get_revision_request','list_revision_runs','insert_revision_run',
            'clone_revision_run','clone_revision_evaluation','get_training_launch_claim','get_revision_launch_claim',
            'insert_revision_launch_claim'):
            setattr(self,name+'_sql',(root/(name+'.sql')).read_text(encoding='utf-8'))
        self.runs = ExperimentRunRepository(password)

    @staticmethod
    def decode_revision(row):
        if row is None: raise RunNotFoundError('修改后运行不存在或不可访问。')
        try:
            result = dict(row)
            text = row['binding_json']
            if len(text.encode())>131072 or content_hash(text)!=row['binding_digest']: raise ValueError('binding digest')
            result['binding'] = strict_json(text)
            if result['binding']['revision']['change_id']!=row['change_id'] or result['binding']['revision']['original_run_id']!=row['original_run_id']:
                raise ValueError('identity')
            result['created_at'] = _utc(row['created_at'])
            return result
        except (ValueError,KeyError,TypeError) as error: raise RunStorageError('修改后运行绑定校验失败。') from error

    @logged_operation('revision_repository.get')
    def get_revision(self,user_id,run_id,*,required=True):
        user_id,run_id=validate_positive_integer(user_id,'用户 ID'),normalize_run_uuid(run_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._owner(cursor,user_id,run_id)
                cursor.execute(self.get_revision_run_sql,(user_id,run_id));row=cursor.fetchone()
                return self.decode_revision(row) if row else (None if not required else self.decode_revision(None))

    @logged_operation('revision_repository.by_request')
    def by_request(self,user_id,request_id,change_id,parameters):
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_revision_request_sql,(user_id,request_id));row=cursor.fetchone()
                if row is None:return None
                result=self.decode_revision(row)
                if row['change_id']!=change_id or result['binding']['overrides']!=parameters:
                    raise RevisionConflict('该请求编号已用于其他修改或参数。')
                return result

    @logged_operation('revision_repository.create')
    def create_revision(self,user_id,change,request_id,overrides,binding,*,chat=None):
        user_id=validate_positive_integer(user_id,'用户 ID');request_id=normalize_run_uuid(request_id)
        text=compact(binding)
        if len(text.encode())>131072:raise ValueError('修改后启动绑定过大。')
        with get_connection(self.password) as connection:
            try:
                question=self._chat(connection,user_id,chat)
                if question is not None:require_revision_start(question,change['change_id'],'')
                with connection.cursor(dictionary=True) as cursor:
                    self._owner(cursor,user_id,change['run_id'],lock=True)
                    cursor.execute(self.get_revision_request_sql,(user_id,request_id));row=cursor.fetchone()
                    if row:
                        result=self.decode_revision(row)
                        if row['change_id']!=change['change_id'] or result['binding']['overrides']!=overrides:
                            raise RevisionConflict('请求编号已用于其他修改或参数。')
                    else:
                        cursor.execute(self.get_code_change_sql,(user_id,change['change_id']))
                        current=CodeChangeRepository._decode(cursor.fetchone())
                        if current['status']!='applied' or current['result_json']!=change['result_json']:
                            raise RevisionConflict('修改尚未完成或检查失败。')
                        cursor.execute(self.runs.get_experiment_run_sql,(user_id,change['run_id']))
                        original=_read_row(cursor.fetchone())
                        if original.status not in {'succeeded','failed','cancelled'} or original.evaluation is None:
                            raise RevisionConflict('原运行必须已结束并有评测声明。')
                        parameters=canonical_parameters(binding['parameters'])
                        if original.parameters|overrides != binding['parameters']:
                            raise RevisionConflict('运行参数来源发生变化。')
                        identifier=uuid4().hex
                        cursor.execute(self.clone_revision_run_sql,(identifier,request_id,
                            run_input_digest(original.experiment_id,parameters),parameters,user_id,original.run_id))
                        cursor.execute(self.clone_revision_evaluation_sql,(identifier,user_id,original.run_id))
                        cursor.execute(self.insert_revision_run_sql,(identifier,user_id,original.run_id,change['change_id'],
                            request_id,text,content_hash(text),chat['session_id'] if chat else None))
                        cursor.execute(self.get_revision_run_sql,(user_id,identifier));result=self.decode_revision(cursor.fetchone())
                connection.commit();return result
            except IntegrityError as error:
                connection.rollback()
                if error.errno==errorcode.ER_DUP_ENTRY:raise RevisionConflict('请求编号已被使用，请查询原记录。') from error
                raise
            except Exception:
                connection.rollback();raise

    @logged_operation('revision_repository.claim_chat')
    def claim_chat(self,user_id,run_id,chat):
        with get_connection(self.password) as connection:
            try:
                question=self._chat(connection,user_id,chat)
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.get_revision_run_sql,(user_id,run_id));record=self.decode_revision(cursor.fetchone())
                    require_revision_start(question,record['change_id'],run_id)
                    cursor.execute(self.get_training_launch_claim_sql,(user_id,chat['session_id'],chat['attempt'].turn_id))
                    if cursor.fetchone():raise RevisionConflict('本轮已经启动普通训练，不能再启动另一运行。')
                    cursor.execute(self.get_revision_launch_claim_sql,(user_id,chat['session_id'],chat['attempt'].turn_id))
                    other=cursor.fetchone()
                    if other and other['run_id']!=run_id:raise RevisionConflict('本轮已经启动另一修改后运行。')
                    if not other:cursor.execute(self.insert_revision_launch_claim_sql,(chat['session_id'],chat['attempt'].turn_id,user_id,run_id))
                connection.commit()
            except Exception:connection.rollback();raise

    @logged_operation('revision_repository.list')
    def list_revisions(self,user_id,change_id,limit=10):
        self.get(user_id,change_id)  # 修改归属校验；空列表不泄露他人修改是否存在。
        if type(limit) is not int or not 1<=limit<=20:raise ValueError('分页范围错误。')
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.list_revision_runs_sql,(user_id,change_id,limit))
                return [self.decode_revision(row) for row in cursor.fetchall()]
