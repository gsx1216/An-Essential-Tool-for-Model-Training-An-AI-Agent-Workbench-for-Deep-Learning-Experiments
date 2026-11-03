"""聊天适配：从落库原文判断模式，真实启动授权仍由仓储事务校验。"""
import asyncio
import logging
from fastapi import HTTPException

from app.errors import RunNotFoundError, RunRequestConflictError
from app.logging_config import log_failure
from app.models.code_change import ChangeNotFound, ChangeStorageError
from app.models.research import content_hash
from app.models.revision_run import RevisionConflict, revision_mode, require_revision_start
from app.models.revision_tools import projection
from app.models.training_task import TrainingInstructionRequired
from app.services.read_only_chat_service import UNAVAILABLE

logger=logging.getLogger(__name__)


class ChatRevisionService:
    def __init__(self,service):self.service=service

    async def mode(self,user_id,session_id,attempt):
        import re
        text=await asyncio.to_thread(self.service.repository.question,user_id,{'session_id':session_id,'attempt':attempt})
        if not revision_mode(text):return False
        for identifier in re.findall(r'(?<![a-f0-9])(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})(?![a-f0-9])',text,re.I):
            try:require_revision_start(text,identifier.replace('-','').lower(),'');return True
            except TrainingInstructionRequired:pass
        return False

    async def execute(self,user_id,session_id,attempt,name,args,bound):
        service=self.service;chat={'session_id':session_id,'attempt':attempt,'bound':bound}
        target=None
        try:
            if name=='prepare_revision_run':
                request=content_hash('revision:'+session_id+':'+attempt.turn_id)[:32]
                row=await service.prepare(user_id,args.change_id.hex,request,args.parameters,chat=chat)
                return {'ok':True,'code':'REVISION_READY','run_id':row['run_id'],'data':projection(row)}
            if name=='start_revision_run':
                target=args.run_id.hex
                result=await service.start(user_id,target,chat=chat)
                return {'ok':True,'code':'START_REQUESTED','run_id':target,'data':{key:result['execution'].get(key) for key in ('run_id','status','exit_code','error_code','output_directory')}}
            if name=='get_revision_run':
                row=await service.get(user_id,args.run_id.hex)
                return {'ok':True,'code':'OK','run_id':row['run_id'],'data':projection(row)}
            if name=='list_revision_runs':
                rows=await service.list(user_id,args.change_id.hex,5)
                return {'ok':True,'code':'OK','data':{'items':[projection(row) for row in rows]}}
            result=await service.compare(user_id,args.run_id.hex)
            result['series_total']=len(result['series']);result['series']=result['series'][:8]
            result['display_truncated']=result['series_total']>8
            result['offset']=0
            result['next_offset']=8 if result['display_truncated'] else None
            return {'ok':True,'code':'OK','run_id':args.run_id.hex,'data':result}
        except (RevisionConflict,TrainingInstructionRequired,RunRequestConflictError):
            return {'ok':False,'code':'REVISION_CONFLICT','data':{'hint':'请明确指定修改 ID/新运行 ID 后启动，并核对版本、参数及部署入口支持。'}}
        except (RunNotFoundError,ChangeNotFound):return {'ok':False,'code':'NOT_FOUND'}
        except ValueError:return {'ok':False,'code':'INVALID_ARGUMENTS'}
        except (*UNAVAILABLE,ChangeStorageError,HTTPException,TimeoutError) as error:
            log_failure(logger,error,operation='chat_revision_service.execute')
            return {'ok':False,'code':'START_UNCONFIRMED' if target else 'REVISION_UNAVAILABLE','run_id':target,
                'data':{'hint':'请查询原记录；若已提交启动，只重试同一 run，不新建运行重跑。'}}
