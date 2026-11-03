"""个人论文原件、状态和分块；查询始终带当前用户，原件不参与检索。"""
from contextlib import contextmanager
import json
from pathlib import Path
from uuid import uuid4

from mysql.connector import IntegrityError,errorcode

from app.database import get_connection
from app.logging_config import logged_operation
from app.models.paper import PaperResponse,PaperNotFound,PaperConflict,PaperStorageError,sha256
from app.models.experiment_run import normalize_run_uuid
from app.repositories.experiment_repository import _utc
from app.validators import validate_positive_integer,normalize_required_text


class PaperRepository:
    def __init__(self,password):
        self.password=password
        root=Path(__file__).resolve().parents[2]/'sql'
        for name in ('paper_fulltext_session','get_paper','get_paper_request','insert_paper','insert_paper_file',
            'get_paper_file','list_papers','claim_paper_retry','insert_paper_chunk','finish_paper','fail_paper',
            'delete_paper','delete_paper_chunks','get_paper_chunk','get_paper_chunk_index','list_paper_chunks','search_paper_chunks'):
            setattr(self,name+'_sql',(root/(name+'.sql')).read_text(encoding='utf-8'))

    @contextmanager
    def connection(self):
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:cursor.execute(self.paper_fulltext_session_sql)
            yield connection

    @staticmethod
    def identity(user_id,paper_id):
        return validate_positive_integer(user_id,'用户 ID'),normalize_run_uuid(paper_id,'论文 ID')

    @staticmethod
    def decode(row):
        if row is None:raise PaperNotFound('论文不存在或不可访问。')
        try:
            result=dict(row);result['paper_id']=row['id'];result['empty_pages']=json.loads(row['empty_pages_json'])
            # 崩溃/重启不会执行失败回调；租约到期后应显示可重试，而非永久解析中。
            if result['status']=='processing' and not result['lease_active']:
                result['status']='failed';result['error_code']='PROCESS_INTERRUPTED'
            for key in ('created_at','updated_at'):result[key]=_utc(row[key])
            PaperResponse(**{key:result[key] for key in PaperResponse.model_fields})
            return result
        except (ValueError,TypeError,KeyError) as error:raise PaperStorageError('论文记录无法核对。') from error

    @staticmethod
    def public(row):return PaperResponse(**{key:row[key] for key in PaperResponse.model_fields}).model_dump(mode='json')

    def _paper(self,cursor,user_id,paper_id,*,lock=False,ready=False):
        cursor.execute(self.get_paper_sql.rstrip().rstrip(';')+(' FOR UPDATE' if lock else ''),(user_id,paper_id))
        row=self.decode(cursor.fetchone())
        if ready and row['status']!='ready':raise PaperConflict('论文尚未完成索引。')
        return row

    @logged_operation('paper_repository.get')
    def get(self,user_id,paper_id):
        user_id,paper_id=self.identity(user_id,paper_id)
        with self.connection() as connection:
            with connection.cursor(dictionary=True) as cursor:return self._paper(cursor,user_id,paper_id)

    @logged_operation('paper_repository.reserve')
    def reserve(self,user_id,request_id,title,raw):
        user_id,request_id=self.identity(user_id,request_id)
        title=normalize_required_text(title,'论文标题',200)
        digest=sha256(raw);identifier,token=uuid4().hex,uuid4().hex
        try:
            with self.connection() as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(self.insert_paper_sql,(identifier,user_id,request_id,title,digest,len(raw),token))
                        cursor.execute(self.insert_paper_file_sql,(identifier,user_id,raw))
                    connection.commit()
                except Exception:connection.rollback();raise
            return self.get(user_id,identifier),token
        except IntegrityError as error:
            if error.errno!=errorcode.ER_DUP_ENTRY:raise
        # 唯一键冲突后用新事务读取，不能从旧的一致性快照查询。
        with self.connection() as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.get_paper_request_sql,(user_id,request_id));record=self.decode(cursor.fetchone())
        if record['file_sha256']!=digest or record['title']!=title or record['file_bytes']!=len(raw):
            raise PaperConflict('同一上传请求编号已用于其他论文或标题。')
        return record,None

    @logged_operation('paper_repository.claim_retry')
    def claim_retry(self,user_id,paper_id,*,rebuild=False):
        user_id,paper_id=self.identity(user_id,paper_id)
        with self.connection() as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    row=self._paper(cursor,user_id,paper_id,lock=True)
                    if row['status']=='ready' and not rebuild:connection.rollback();return row,None
                    if row['lease_active']:raise PaperConflict('论文仍在处理中。')
                    token=uuid4().hex;cursor.execute(self.claim_paper_retry_sql,(token,user_id,paper_id))
                    row=self._paper(cursor,user_id,paper_id)
                connection.commit();return row,token
            except Exception:connection.rollback();raise

    @logged_operation('paper_repository.file',log_success=False)
    def file(self,user_id,paper_id):
        user_id,paper_id=self.identity(user_id,paper_id)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.get_paper_file_sql,(user_id,paper_id));row=cursor.fetchone()
        if not row:raise PaperNotFound('论文不存在或不可访问。')
        raw=bytes(row[0])
        if len(raw)!=row[2] or sha256(raw)!=row[1]:raise PaperStorageError('论文原件校验失败。')
        return raw

    @logged_operation('paper_repository.finish')
    def finish(self,user_id,paper_id,token,data):
        user_id,paper_id=self.identity(user_id,paper_id)
        with self.connection() as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    row=self._paper(cursor,user_id,paper_id,lock=True)
                    if row['status']!='processing' or row['lease_token']!=token or not row['lease_active']:
                        raise PaperConflict('论文解析尝试已失效。')
                    # 重建时旧索引和新索引在同一事务替换；失败回滚保留旧块。
                    cursor.execute(self.delete_paper_chunks_sql,(user_id,paper_id))
                    cursor.executemany(self.insert_paper_chunk_sql,[(uuid4().hex,paper_id,user_id,
                        item['chunk_index'],item['page_number'],item['char_start'],item['content'],item['content_sha256']) for item in data['chunks']])
                    cursor.execute(self.finish_paper_sql,(data['page_count'],len(data['chunks']),json.dumps(data['empty_pages']),data['parser_version'],user_id,paper_id,token))
                    if cursor.rowcount!=1:raise PaperConflict('论文解析尝试已失效。')
                connection.commit()
            except Exception:connection.rollback();raise

    @logged_operation('paper_repository.fail')
    def fail(self,user_id,paper_id,token,code):
        allowed={'INVALID_PDF','ENCRYPTED_PDF','NO_EXTRACTABLE_TEXT','PDF_LIMIT','PDF_TIMEOUT','PARSER_UNAVAILABLE','PROCESS_INTERRUPTED'}
        if code not in allowed:raise ValueError('论文失败分类不正确。')
        with self.connection() as connection:
            try:
                with connection.cursor() as cursor:cursor.execute(self.fail_paper_sql,(code,user_id,paper_id,token))
                connection.commit()
            except Exception:connection.rollback();raise

    @logged_operation('paper_repository.delete')
    def delete(self,user_id,paper_id):
        user_id,paper_id=self.identity(user_id,paper_id)
        with self.connection() as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    row=self._paper(cursor,user_id,paper_id,lock=True)
                    if row['lease_active']:raise PaperConflict('处理中不能删除，请完成或过期后再试。')
                    cursor.execute(self.delete_paper_sql,(user_id,paper_id))
                connection.commit()
            except Exception:connection.rollback();raise

    @logged_operation('paper_repository.list')
    def list(self,user_id,*,offset=0,limit=20):
        user_id=validate_positive_integer(user_id,'用户 ID')
        if type(offset) is not int or not 0<=offset<=10000 or type(limit) is not int or not 1<=limit<=20:raise ValueError('论文分页范围不正确。')
        with self.connection() as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.list_papers_sql,(user_id,limit+1,offset));rows=cursor.fetchall()
        items=[self.public(self.decode(row)) for row in rows[:limit]]
        return {'items':items,'offset':offset,'next_offset':offset+len(items) if len(rows)>limit else None}

    @logged_operation('paper_repository.search',log_success=False)
    def search(self,user_id,args):
        user_id=validate_positive_integer(user_id,'用户 ID');paper_id=args.paper_id.hex if args.paper_id else None
        with self.connection() as connection:
            with connection.cursor(dictionary=True) as cursor:
                if paper_id:self._paper(cursor,user_id,paper_id,ready=True)
                cursor.execute(self.search_paper_chunks_sql,(args.query,user_id,paper_id,paper_id,args.query,args.limit))
                return cursor.fetchall()

    @logged_operation('paper_repository.chunk',log_success=False)
    def chunk(self,user_id,args):
        user_id=validate_positive_integer(user_id,'用户 ID')
        with self.connection() as connection:
            with connection.cursor(dictionary=True) as cursor:
                if args.paper_id:
                    self._paper(cursor,user_id,args.paper_id.hex,ready=True)
                    cursor.execute(self.get_paper_chunk_index_sql,(user_id,args.paper_id.hex,args.chunk_index))
                else:cursor.execute(self.get_paper_chunk_sql,(user_id,args.chunk_id.hex))
                row=cursor.fetchone()
        if not row:raise PaperNotFound('论文片段不存在或不可访问。')
        if sha256(row['content_text'].encode())!=row['content_sha256']:raise PaperStorageError('论文片段校验失败。')
        return row

    @logged_operation('paper_repository.chunks',log_success=False)
    def chunks(self,user_id,paper_id,*,offset=0,limit=20):
        user_id,paper_id=self.identity(user_id,paper_id)
        if type(offset) is not int or not 0<=offset<=3000 or type(limit) is not int or not 1<=limit<=20:raise ValueError('论文分块分页范围不正确。')
        with self.connection() as connection:
            with connection.cursor(dictionary=True) as cursor:
                self._paper(cursor,user_id,paper_id,ready=True)
                cursor.execute(self.list_paper_chunks_sql,(user_id,paper_id,limit+1,offset));rows=cursor.fetchall()
        return rows[:limit],offset+limit if len(rows)>limit else None
