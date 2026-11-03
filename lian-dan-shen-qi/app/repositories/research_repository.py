"""本人聊天中的不可变资料快照与已校验建议；不跨网络持有事务。"""
import asyncio
import json
from pathlib import Path
from uuid import UUID

from app.database import get_connection
from app.errors import ChatToolStorageError
from app.logging_config import logged_operation
from app.models.chat_tools import compact_json
from app.models.research import KINDS,MAX_CONTENT,canonical_url,content_hash
from app.repositories.context_repository import ContextRepository
from app.repositories.experiment_repository import _utc
from app.validators import normalize_session_id,validate_positive_integer


def reference_values(row,user_id,session_id,attempt):
    identifier=UUID(row['reference_id']).hex
    if row['kind'] not in KINDS or canonical_url(row['url'])!=row['url']:raise ValueError('invalid reference')
    if not 1<=len(row['content'].encode())<=MAX_CONTENT or content_hash(row['content'])!=row['content_sha256']:
        raise ValueError('invalid reference digest')
    metadata=compact_json(row['metadata'])
    if len(metadata.encode())>4096:raise ValueError('reference metadata budget')
    return (identifier,session_id,user_id,attempt.turn_id,attempt.attempt_id,row['run_id'],row['url'],row['title'],
            row['kind'],row['content'],row['content_sha256'],metadata)


class ResearchRepository(ContextRepository):
    def __init__(self,password):
        super().__init__(password)
        root=Path(__file__).resolve().parents[2]/'sql'
        for name in ('get_research_reference','list_research_references','list_research_advice',
                     'init_research_rate','lock_research_rate','update_research_rate'):
            setattr(self,name+'_sql',(root/(name+'.sql')).read_text(encoding='utf-8'))

    @logged_operation('research_repository.get_reference')
    def get_reference(self,user_id,session_id,reference_id,*,attempt=None):
        user_id=validate_positive_integer(user_id,'用户 ID');session_id=normalize_session_id(session_id)
        reference_id=UUID(reference_id).hex
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    self._require_session(cursor,user_id,session_id)
                    if attempt is not None:self._require_attempt(cursor,session_id,attempt)
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.get_research_reference_sql,(user_id,session_id,reference_id));row=cursor.fetchone()
                    if row:
                        if content_hash(row['content'])!=row['content_sha256']:raise ChatToolStorageError('资料快照校验失败。')
                        try:row['metadata']=json.loads(row.pop('metadata_json'))
                        except (ValueError,TypeError) as error:raise ChatToolStorageError('资料元信息校验失败。') from error
                        if type(row['metadata']) is not dict:raise ChatToolStorageError('资料元信息校验失败。')
                        row['retrieved_at']=_utc(row['retrieved_at'])
                connection.commit();return row
            except Exception:connection.rollback();raise

    @logged_operation('research_repository.list_saved')
    def list_saved(self,user_id,session_id,*,limit=20,advice=False):
        user_id=validate_positive_integer(user_id,'用户 ID');session_id=normalize_session_id(session_id)
        if type(limit) is not int or not 1<=limit<=50:raise ValueError('每次只读 1～50 条资料。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:self._require_session(cursor,user_id,session_id)
                with connection.cursor(dictionary=True) as cursor:
                    cursor.execute(self.list_research_advice_sql if advice else self.list_research_references_sql,(user_id,session_id,limit))
                    rows=cursor.fetchall()
                    for row in rows:
                        if advice:
                            if content_hash(row['advice_json'])!=row['advice_sha256']:raise ChatToolStorageError('建议快照校验失败。')
                            try:row['advice']=json.loads(row.pop('advice_json'))
                            except (ValueError,TypeError) as error:raise ChatToolStorageError('建议内容校验失败。') from error
                            row['created_at']=_utc(row['created_at'])
                        else:row['retrieved_at']=_utc(row['retrieved_at'])
                connection.commit();return {'items':rows}
            except Exception:connection.rollback();raise

    def reserve_request(self):
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(self.init_research_rate_sql)
                    cursor.execute(self.lock_research_rate_sql);delay=float(cursor.fetchone()[0])
                    if delay<=0:cursor.execute(self.update_research_rate_sql)
                connection.commit();return delay
            except Exception:connection.rollback();raise

    async def permit(self):
        from app.models.research import ResearchUnavailableError
        for _ in range(4):
            delay=await asyncio.to_thread(self.reserve_request)
            if delay<=0:return
            if delay>3.5:break
            await asyncio.sleep(delay+.01)
        raise ResearchUnavailableError('资料请求频率过高，请稍后重试。')
