"""聊天适配：当前原文授权在仓储事务内检查，不接受模型提供 request_id 或 authorized。"""
import asyncio
from fastapi import HTTPException
from mysql.connector import Error as MySQLError
from openai import OpenAIError

from app.errors import RunNotFoundError, ModelCredentialDecryptionError
from app.models.research import content_hash
from app.models.code_change import ChangeNotFound, ChangeConflict, ChangeInstructionRequired, ChangeProtocolError, ChangeStorageError
from app.models.code_change_tools import projection
from app.models.source_code import SourceProtocolError
from app.runner.source_reader import SourceReadError
from app.services.read_only_chat_service import UNAVAILABLE
from app.logging_config import log_failure
import logging

logger = logging.getLogger(__name__)


class ChatCodeChangeService:
    def __init__(self, changes): self.changes = changes

    async def mode(self, user_id, session_id, attempt):
        return await self.changes.mode(user_id, {'session_id':session_id,'attempt':attempt})

    async def execute(self, user_id, session_id, attempt, name, args, bound):
        chat = {'session_id':session_id,'attempt':attempt,'bound':bound}
        try:
            if name == 'list_code_changes':
                target = args.run_id or bound
                if target is None: return {'ok':False,'code':'NO_LINKED_RUN'}
                rows = await self.changes.list(user_id,target,limit=5)
                return {'ok':True,'code':'OK','run_id':target,'data':{'items':[
                    {key: row[key] for key in ('change_id','run_id','suggestion_index','status','error_code')} for row in rows]}}
            if name == 'prepare_code_change':
                target = args.run_id or bound
                if target is None: return {'ok':False,'code':'NO_LINKED_RUN'}
                request_id = content_hash('code-change:'+session_id+':'+attempt.turn_id)[:32]
                row = await self.changes.prepare(user_id,target,args.suggestion_task_id.hex,args.suggestion_index,request_id,chat=chat)
            elif name == 'apply_code_change':
                row = await self.changes.apply(user_id,args.change_id.hex,chat=chat)
            else:
                row = await self.changes.get(user_id,args.change_id.hex)
            return {'ok':True,'code':'OK','run_id':row['run_id'],'data':projection(row)}
        except (ChangeInstructionRequired, ChangeConflict):
            return {'ok':False,'code':'CHANGE_CONFLICT','data':{'hint':'请明确应用第几条建议或修改 ID，并确认当前运行及文件版本。'}}
        except (ChangeNotFound, RunNotFoundError):
            return {'ok':False,'code':'NOT_FOUND'}
        except (MySQLError, OpenAIError, ChangeProtocolError, ChangeStorageError, SourceReadError, SourceProtocolError,
                ModelCredentialDecryptionError, HTTPException, *UNAVAILABLE) as error:
            log_failure(logger,error,operation='chat_code_change_service.execute')
            return {'ok':False,'code':'CHANGE_UNAVAILABLE','data':{'hint':'修改未确认完成，请查询同一修改记录后重试；未启动训练。'}}
