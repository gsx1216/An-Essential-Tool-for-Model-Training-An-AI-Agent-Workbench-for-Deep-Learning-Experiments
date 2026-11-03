"""会话运行关联和工具审计；同一会话锁下校验当前生成尝试，网络等待不持锁。"""
import json
from pathlib import Path
import re

from app.database import get_connection
from app.errors import ChatToolStorageError, RunNotFoundError
from app.logging_config import logged_operation
from app.models.chat_tools import ARGUMENTS, compact_json
from app.models.research import RESEARCH_ARGUMENTS
from app.models.training_task import TRAINING_ARGUMENTS
from app.models.analysis_tools import ANALYSIS_ARGUMENTS
from app.models.suggestion_tools import SUGGESTION_ARGUMENTS
from app.models.code_change_tools import CHANGE_ARGUMENTS
from app.models.revision_tools import REVISION_READ,REVISION_WRITE
from app.models.source_code import SOURCE_ARGUMENTS
from app.models.paper import PAPER_ARGUMENTS,PAPER_AUDIT_BYTES
from app.models.history_recall import HISTORY_ARGUMENTS
from app.repositories.research_repository import reference_values
from app.models.context_snapshot import ContextAttempt
from app.models.experiment_run import normalize_run_uuid
from app.repositories.context_repository import ContextRepository
from app.repositories.experiment_repository import _utc
from app.validators import normalize_session_id, validate_positive_integer


class ChatToolRepository(ContextRepository):
    def __init__(self,password):
        super().__init__(password)
        root=Path(__file__).resolve().parents[2]/'sql'
        for name in ('get_chat_run_binding','save_chat_run_binding','clear_chat_run_binding',
                     'save_chat_tool_call','list_chat_tool_calls','chat_tool_owned_run','save_research_reference','search_chat_history','read_chat_history'):
            setattr(self,name+'_sql',(root/(name+'.sql')).read_text(encoding='utf-8'))

    @staticmethod
    def _identity(user_id,session_id):
        return validate_positive_integer(user_id,'用户 ID'),normalize_session_id(session_id)

    @logged_operation('chat_tool_repository.recall_history',log_success=False)
    def recall_history(self,user_id,session_id,attempt,name,args):
        user_id,session_id=self._identity(user_id,session_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor,user_id,session_id)
                    self._require_attempt(cursor,session_id,attempt)
                    tail=self._tail_seq(cursor,session_id)
                    if name=='search_chat_history':
                        bound=min(tail,args.before_seq or tail)
                        cursor.execute(self.search_chat_history_sql,(args.query,user_id,session_id,bound,args.query,args.limit))
                        rows=cursor.fetchall()
                        data={'items':[{'seq':row[0],'role':row[1],'preview':row[2]} for row in rows],
                              'next_before_seq':rows[-1][0] if len(rows)==args.limit else None,
                              'notice':'关键词原文片段；先读取完整相关消息再回答，不代表事实已核实。'}
                    else:
                        cursor.execute(self.read_chat_history_sql,(args.offset+1,user_id,session_id,args.seq,tail))
                        row=cursor.fetchone()
                        data=None if row is None else {'seq':args.seq,'role':row[0],'text':row[1],
                            'offset':args.offset,'content_length':row[2],'reference_id':f'history_{args.seq}',
                            'next_offset':args.offset+len(row[1]) if args.offset+len(row[1])<row[2] else None}
                connection.commit()
            except Exception:connection.rollback();raise
        return {'ok':data is not None,'code':'OK' if data is not None else 'HISTORY_NOT_FOUND','data':data or {}}

    @logged_operation('chat_tool_repository.binding')
    def binding(self,user_id,session_id,attempt):
        user_id,session_id=self._identity(user_id,session_id)
        if not isinstance(attempt,ContextAttempt):raise ValueError('缺少当前生成尝试。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor,user_id,session_id)
                    self._require_attempt(cursor,session_id,attempt)
                    cursor.execute(self.get_chat_run_binding_sql,(user_id,session_id))
                    row=cursor.fetchone()
                connection.commit()
                return normalize_run_uuid(row[0]) if row else None
            except Exception:
                connection.rollback();raise

    @logged_operation('chat_tool_repository.record')
    def record(self,user_id,session_id,attempt,*,call_id,call_index,name,arguments,outcome,metadata,duration_ms,binding='keep',run_id=None,reference_rows=None):
        user_id,session_id=self._identity(user_id,session_id)
        if (not isinstance(attempt,ContextAttempt) or not isinstance(call_id,str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',call_id) or type(call_index) is not int or not 1<=call_index<=6
            or name not in {*ARGUMENTS,*RESEARCH_ARGUMENTS,*TRAINING_ARGUMENTS,*ANALYSIS_ARGUMENTS,*SOURCE_ARGUMENTS,*SUGGESTION_ARGUMENTS,*CHANGE_ARGUMENTS,*REVISION_READ,*REVISION_WRITE,*PAPER_ARGUMENTS,*HISTORY_ARGUMENTS,'unsupported'} or not re.fullmatch('[A-Z_]{1,32}',outcome)
            or binding not in {'keep','set','clear'} or type(duration_ms) is not int or not 0<=duration_ms<=3600000):
            raise ValueError('工具记录不符合要求。')
        text,meta=compact_json(arguments),compact_json(metadata)
        meta_limit=PAPER_AUDIT_BYTES if name=='read_paper_chunk' else 4096
        if len(text.encode())>2048 or len(meta.encode())>meta_limit:raise ValueError('工具记录超过预算。')
        if binding=='set':run_id=normalize_run_uuid(run_id)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor,user_id,session_id)
                    self._require_attempt(cursor,session_id,attempt)
                    if binding=='set':
                        cursor.execute(self.chat_tool_owned_run_sql,(user_id,run_id))
                        if cursor.fetchone() is None:raise RunNotFoundError('运行不存在或不属于当前用户。')
                    for row in reference_rows or []:
                        cursor.execute(self.save_research_reference_sql,reference_values(row,user_id,session_id,attempt))
                    cursor.execute(self.save_chat_tool_call_sql,(session_id,user_id,attempt.turn_id,attempt.attempt_id,
                                   call_id,call_index,name,text,outcome,meta,duration_ms))
                    if binding=='set':cursor.execute(self.save_chat_run_binding_sql,(session_id,user_id,run_id))
                    elif binding=='clear':cursor.execute(self.clear_chat_run_binding_sql,(user_id,session_id))
                connection.commit()
            except Exception:
                connection.rollback();raise

    @logged_operation('chat_tool_repository.list_calls')
    def list_calls(self,user_id,session_id,*,after_id=0,limit=20):
        user_id,session_id=self._identity(user_id,session_id)
        if type(after_id) is not int or not 0<=after_id<2**64 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('工具记录分页不符合要求。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor,user_id,session_id)
                    cursor.execute(self.get_chat_run_binding_sql,(user_id,session_id));bound=cursor.fetchone()
                    cursor.execute(self.list_chat_tool_calls_sql,(user_id,session_id,after_id,limit+1));rows=cursor.fetchall()
                    try:
                        items=[dict(zip(('id','turn_id','attempt_id','call_id','call_index','tool_name','arguments','outcome','result_metadata','duration_ms','created_at'),
                                        (*row[:6],json.loads(row[6]),row[7],json.loads(row[8]),row[9],_utc(row[10])))) for row in rows[:limit]]
                    except (ValueError,TypeError,IndexError) as error:raise ChatToolStorageError('工具记录读取失败。') from error
                connection.commit()
                return {'active_run_id':bound[0] if bound else None,'items':items,'next_after_id':items[-1]['id'] if len(rows)>limit else None}
            except Exception:
                connection.rollback();raise
