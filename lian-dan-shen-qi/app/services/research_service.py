"""公开研究工具：网络结果先形成待提交快照，随工具审计同事务保存。"""
import asyncio
import logging
from datetime import datetime,timezone

from mysql.connector import Error as MySQLError

from app.logging_config import logged_operation,log_failure
from app.models.chat_tools import clip
from app.models.research import ResearchUnavailableError,render_grounded_reply

logger=logging.getLogger(__name__)


class ResearchService:
    def __init__(self,repository,client):
        self.repository,self.client=repository,client

    async def hints(self,user_id,session_id):
        rows=await asyncio.to_thread(self.repository.list_saved,user_id,session_id,limit=2)
        return [{'reference_id':row['reference_id'],'title':clip(row['title'],80),'kind':row['kind']} for row in rows['items']]

    async def list_saved(self,user_id,session_id,**options):
        return await asyncio.to_thread(self.repository.list_saved,user_id,session_id,**options)

    @logged_operation('research_service.execute')
    async def execute(self,user_id,session_id,attempt,name,args,run_id):
        try:
            if name=='search_references':
                rows=await self.client.search(args.query,args.limit,run_id)
                items=[{'reference_id':row['reference_id'],'title':clip(row['title'],150),'url':row['url'],
                        'kind':row['kind'],'abstract_preview':clip(row['content'],140)} for row in rows]
                return {'ok':True,'code':'OK' if rows else 'NO_REFERENCES','data':{'items':items,'abstract_only':True}},rows
            rows=[]
            if args.reference_id:
                row=await asyncio.to_thread(self.repository.get_reference,user_id,session_id,args.reference_id,attempt=attempt)
                if row is None:return {'ok':False,'code':'REFERENCE_NOT_FOUND'},[]
                # Try arXiv HTML once, then explicitly fall back to the saved abstract.
                if row['kind']=='search_abstract' and args.offset==0:
                    try:
                        row=await self.client.read(row['url'].replace('/abs/','/html/'),run_id)
                        rows=[row]
                    except ResearchUnavailableError:pass
            else:
                row=await self.client.read(args.url,run_id);rows=[row]
            excerpt=clip(row['content'][args.offset:],args.max_bytes)
            end=args.offset+len(excerpt)
            data={'reference_id':row['reference_id'],'url':row['url'],'title':clip(row['title'],150),
                  'kind':row['kind'],'content_sha256':row['content_sha256'],'offset':args.offset,'text':excerpt,
                  'next_offset':end if end<len(row['content']) else None,'content_length':len(row['content']),
                  'retrieved_at':row.get('retrieved_at',datetime.now(timezone.utc)).isoformat(),
                  'abstract_only':row['kind'] in {'search_abstract','paper_abstract'},
                  'snapshot_truncated':row.get('metadata',{}).get('normalized_text_capped',False),
                  'notice':'规范化文本片段，可能仅为正文前缀；字符位置不是 PDF 页码，摘要不能作为实施细节的正文依据。'}
            return {'ok':True,'code':'OK','data':data},rows
        except (ResearchUnavailableError,MySQLError) as error:
            log_failure(logger,error,operation='research_service.public_read',level=logging.WARNING)
            return {'ok':False,'code':'RESEARCH_UNAVAILABLE','data':{'hint':'未取得可核验资料；没有替换成模型编造的来源。'}},[]

    @staticmethod
    def remember(result,delivered):
        data=result.get('data',{})
        if result.get('ok') and data.get('reference_id') and data.get('text'):
            delivered.setdefault(data['reference_id'],[]).append(data.copy())

    @staticmethod
    def finish(content,delivered,run_id):
        if not delivered:
            return '本次没有取得并读取可核验的资料正文，暂不形成有依据的修改建议。检索无结果、来源不可用或仅有检索摘要时，都不能由模型补造依据。'
        return render_grounded_reply(content,delivered,run_id)
