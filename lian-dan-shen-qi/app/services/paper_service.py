"""上传/解析与检索业务；MySQL 原件和分块持久化，模型只收到有界片段。"""
import asyncio
from datetime import datetime,timezone
import logging

from mysql.connector import Error as MySQLError

from app.logging_config import logged_operation,log_failure
from app.models.chat_tools import clip
from app.models.paper import (MAX_PDF_BYTES,PaperTooLarge,PaperUnsupported,PaperBusy,PaperParseError,
    PaperConflict,PaperNotFound,PaperStorageError,SearchPapers,ReadPaperChunk)
from app.services.pdf_parser import PdfParser

logger=logging.getLogger(__name__)


def hit(row):
    return {'paper_id':row['paper_id'],'chunk_id':row['id'],'title':row['title'],'page_number':row['page_number'],
        'chunk_index':row['chunk_index'],'preview':clip(row['content_text'],140)}


class PaperService:
    def __init__(self,repository,*,parser=None):
        self.repository=repository;self.parser=parser or PdfParser();self.slots=asyncio.Semaphore(2)

    async def _admit(self):
        if self.slots.locked():raise PaperBusy('论文处理繁忙，请稍后重试。')
        await self.slots.acquire()

    async def _process(self,user_id,record,token,raw):
        if token is None:return self.repository.public(record)
        try:
            data=await self.parser.parse(raw)
            await asyncio.to_thread(self.repository.finish,user_id,record['paper_id'],token,data)
        except PaperParseError as error:
            log_failure(logger,error,operation='paper_service.parse',level=logging.WARNING)
            await asyncio.to_thread(self.repository.fail,user_id,record['paper_id'],token,error.code)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self.repository.fail,user_id,record['paper_id'],token,'PROCESS_INTERRUPTED'))
            raise
        except Exception as error:
            log_failure(logger,error,operation='paper_service.parse')
            await asyncio.to_thread(self.repository.fail,user_id,record['paper_id'],token,'PROCESS_INTERRUPTED')
            raise PaperStorageError('论文处理暂时不可用。') from error
        return await self.get(user_id,record['paper_id'])

    @logged_operation('paper_service.upload')
    async def upload(self,user_id,request_id,title,stream):
        await self._admit()
        try:
            raw=bytearray()
            async with asyncio.timeout(90):
                async for piece in stream:
                    if len(raw)+len(piece)>MAX_PDF_BYTES:raise PaperTooLarge('论文最多 10 MiB。')
                    raw.extend(piece)
                if len(raw)<6 or not raw.startswith(b'%PDF-'):raise PaperUnsupported('需要 PDF 原始二进制正文。')
                raw=bytes(raw)
                record,token=await asyncio.to_thread(self.repository.reserve,user_id,request_id,title,raw)
                return await self._process(user_id,record,token,raw)
        finally:self.slots.release()

    @logged_operation('paper_service.retry')
    async def retry(self,user_id,paper_id,*,rebuild=False):
        await self._admit()
        try:
            async with asyncio.timeout(90):
                record,token=await asyncio.to_thread(self.repository.claim_retry,user_id,paper_id,rebuild=rebuild)
                if token is None:return self.repository.public(record)
                raw=await asyncio.to_thread(self.repository.file,user_id,paper_id)
                return await self._process(user_id,record,token,raw)
        finally:self.slots.release()

    @logged_operation('paper_service.get')
    async def get(self,user_id,paper_id):
        return self.repository.public(await asyncio.to_thread(self.repository.get,user_id,paper_id))

    @logged_operation('paper_service.list')
    async def list(self,user_id,**options):return await asyncio.to_thread(self.repository.list,user_id,**options)

    @logged_operation('paper_service.delete')
    async def delete(self,user_id,paper_id):return await asyncio.to_thread(self.repository.delete,user_id,paper_id)

    async def file(self,user_id,paper_id):return await asyncio.to_thread(self.repository.file,user_id,paper_id)

    @logged_operation('paper_service.search',log_success=False)
    async def search(self,user_id,args):
        args=SearchPapers.model_validate(args)
        return {'items':[hit(row) for row in await asyncio.to_thread(self.repository.search,user_id,args)],
            'retrieval':'mysql_ngram_fulltext','notice':'关键词检索结果；先读取片段再引用，不代表已读完整论文。'}

    @logged_operation('paper_service.read',log_success=False)
    async def read(self,user_id,args):
        args=ReadPaperChunk.model_validate(args)
        row=await asyncio.to_thread(self.repository.chunk,user_id,args)
        text=clip(row['content_text'][args.offset:],args.max_bytes);end=args.offset+len(text)
        return {'paper_id':row['paper_id'],'chunk_id':row['id'],'reference_id':row['id'],'title':row['title'],
            'kind':'uploaded_pdf','url':None,'page_number':row['page_number'],'chunk_index':row['chunk_index'],
            'page_char_start':row['char_start'],'offset':args.offset,'text':text,'content_length':len(row['content_text']),
            'next_offset':end if end<len(row['content_text']) else None,
            'previous_chunk_index':row['chunk_index']-1 if row['chunk_index']>0 else None,
            'next_chunk_index':row['chunk_index']+1 if row['chunk_index']+1<row['chunk_count'] else None,
            'content_sha256':row['content_sha256'],'file_sha256':row['file_sha256'],
            'retrieved_at':datetime.now(timezone.utc).isoformat(),'abstract_only':False}

    async def chunks(self,user_id,paper_id,**options):
        rows,next_offset=await asyncio.to_thread(self.repository.chunks,user_id,paper_id,**options)
        return {'items':[hit(row) for row in rows],'offset':options.get('offset',0),'next_offset':next_offset}

    async def tool(self,user_id,name,args):
        try:
            if name=='list_papers':data=await self.list(user_id,**args.model_dump())
            elif name=='search_papers':data=await self.search(user_id,args)
            else:data=await self.read(user_id,args)
            return {'ok':True,'code':'OK' if name!='search_papers' or data['items'] else 'NO_MATCH','data':data}
        except PaperNotFound:return {'ok':False,'code':'PAPER_NOT_FOUND'}
        except PaperConflict:return {'ok':False,'code':'PAPER_NOT_READY'}
        except (MySQLError,PaperStorageError) as error:
            log_failure(logger,error,operation='paper_service.tool')
            return {'ok':False,'code':'PAPER_UNAVAILABLE'}
