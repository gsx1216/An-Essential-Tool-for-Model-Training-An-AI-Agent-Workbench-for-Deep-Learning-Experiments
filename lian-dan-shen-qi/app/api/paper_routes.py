"""个人 PDF 接口：原始二进制流上传，不解析 multipart 或接收服务器路径。"""
import logging
from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException,Query,Request,Response
from mysql.connector import Error as MySQLError
from starlette.requests import ClientDisconnect

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_paper_service
from app.api.schemas import UserResponse
from app.models.paper import (PaperResponse,PaperPage,SearchPapers,PaperSearchResponse,ChunkResponse,ChunkHit,
    ReadPaperChunk,PaperNotFound,PaperConflict,PaperTooLarge,PaperUnsupported,PaperBusy,PaperStorageError,Contract)
from app.logging_config import log_failure

router=APIRouter(prefix='/papers',tags=['个人论文知识库与 RAG'])
logger=logging.getLogger(__name__)
ERRORS={400:{'description':'请求不符合要求。'},401:{'description':'请重新登录。'},404:{'description':'论文或片段不存在或不可访问。'},
    409:{'description':'请求编号冲突或论文仍在处理。'},413:{'description':'PDF 超过 10 MiB。'},415:{'description':'需要原始 PDF 请求正文。'},
    429:{'description':'解析繁忙。'},503:{'description':'请稍后重试。'},504:{'description':'处理超时，请查询原记录。'}}


class ChunkPage(Contract):
    items:list[ChunkHit]
    offset:int
    next_offset:int | None


async def operate(response,method,*args,**kwargs):
    try:value=await method(*args,**kwargs)
    except (ValueError,MySQLError,PaperStorageError,PaperBusy,TimeoutError,ClientDisconnect) as error:
        if isinstance(error,PaperNotFound):status,text=404,'论文或片段不存在或不可访问。'
        elif isinstance(error,PaperConflict):status,text=409,'上传请求编号冲突或论文仍在处理，请查看原记录。'
        elif isinstance(error,PaperTooLarge):status,text=413,'PDF 最多 10 MiB。'
        elif isinstance(error,PaperUnsupported):status,text=415,'请上传 application/pdf 类型的原始 PDF 文件。'
        elif isinstance(error,PaperBusy):status,text=429,'论文解析繁忙，请稍后重试。'
        elif isinstance(error,TimeoutError):status,text=504,'论文处理超时，请查看原记录后重试。'
        elif isinstance(error,(ValueError,ClientDisconnect)):status,text=400,'请求参数不符合要求或上传已中断。'
        else:status,text=503,'请稍后重试。'
        log_failure(logger,error,operation='paper_routes',level=logging.ERROR if status>=500 else logging.WARNING)
        raise HTTPException(status,text) from error
    response.headers['Cache-Control']='no-store'
    return value


PDF_BODY={'requestBody':{'required':True,'content':{'application/pdf':{'schema':{'type':'string','format':'binary'}}}}}


@router.post('',status_code=201,response_model=PaperResponse,responses=ERRORS,openapi_extra=PDF_BODY,
    summary='上传本人文字 PDF 并解析分块；不调用模型')
async def upload(request:Request,response:Response,request_id:UUID=Query(description='新上传生成新 UUID，同次上传重试复用。'),
    title:str=Query(min_length=1,max_length=200),user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/pdf':
        raise HTTPException(415,'请上传 application/pdf 类型的原始 PDF 文件。')
    return await operate(response,service.upload,user.user_id,request_id.hex,title,request.stream())


@router.get('',response_model=PaperPage,responses=ERRORS,summary='列出本人上传的论文和索引状态')
async def papers(response:Response,offset:int=Query(0,ge=0,le=10000),limit:int=Query(20,ge=1,le=20),
    user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    return await operate(response,service.list,user.user_id,offset=offset,limit=limit)


@router.post('/search',response_model=PaperSearchResponse,responses=ERRORS,summary='中英文关键词检索本人 ready 论文片段；不调用模型')
async def search(request:SearchPapers,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    return await operate(response,service.search,user.user_id,request)


@router.get('/chunks/{chunk_id}',response_model=ChunkResponse,responses=ERRORS,summary='读取本人论文的有界正文、物理页码和版本')
async def chunk(chunk_id:UUID,response:Response,offset:int=Query(0,ge=0,le=6000),max_bytes:int=Query(12000,ge=256,le=18000),
    user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    return await operate(response,service.read,user.user_id,ReadPaperChunk(chunk_id=chunk_id,offset=offset,max_bytes=max_bytes))


@router.get('/{paper_id}/chunks',response_model=ChunkPage,responses=ERRORS,summary='按论文顺序分页查看本人分块编号及预览')
async def chunks(paper_id:UUID,response:Response,offset:int=Query(0,ge=0,le=3000),limit:int=Query(20,ge=1,le=20),
    user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    return await operate(response,service.chunks,user.user_id,paper_id.hex,offset=offset,limit=limit)


@router.get('/{paper_id}/file',responses={**ERRORS,200:{'content':{'application/pdf':{'schema':{'type':'string','format':'binary'}}}}},
    response_class=Response,summary='下载本人保存的 PDF 原件')
async def file(paper_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    raw=await operate(response,service.file,user.user_id,paper_id.hex)
    return Response(raw,media_type='application/pdf',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Content-Disposition':'attachment; filename="'+paper_id.hex+'.pdf"'})


@router.post('/{paper_id}/retry',response_model=PaperResponse,responses=ERRORS,summary='重试失败或已过期的 PDF 解析；ready 不重复解析')
async def retry(paper_id:UUID,response:Response,rebuild:bool=Query(False,description='true 将已有 ready 论文用当前切分器重建；原件不变，旧片段 ID 失效。'),user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    return await operate(response,service.retry,user.user_id,paper_id.hex,rebuild=rebuild)


@router.delete('/{paper_id}',status_code=204,responses=ERRORS,summary='删除本人 PDF 原件、分块和检索记录')
async def delete(paper_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    await operate(response,service.delete,user.user_id,paper_id.hex)
    return Response(status_code=204,headers={'Cache-Control':'no-store'})


@router.get('/{paper_id}',response_model=PaperResponse,responses=ERRORS,summary='查询本人论文状态和解析结果')
async def paper(paper_id:UUID,response:Response,user:UserResponse=Depends(require_current_user),service=Depends(get_paper_service)):
    return await operate(response,service.get,user.user_id,paper_id.hex)
