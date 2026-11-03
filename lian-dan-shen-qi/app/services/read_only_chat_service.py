"""有界聊天工具循环：按需查询、研究及显式训练；身份始终来自服务端。"""
import asyncio
import logging
import re
from time import perf_counter

from mysql.connector import Error as MySQLError

from app.errors import (ChatToolBudgetError, ChatToolProtocolError, ExperimentNotFoundError,
    MetricStorageError, ReportNotFoundError, RunNotFoundError, RunStorageError, RunnerNotStartedError,
    RunnerProtocolError, RunnerConflictError, ServerNotFoundError, ServerStorageError,
    ServerConnectionError, ServerConnectionTimeoutError, ServerAuthenticationError, ServerHostKeyError,
    ServerAddressNotAllowedError, ServerBusyError, ServerCredentialDecryptionError)
from app.logging_config import logged_operation, log_failure
from app.models.chat_tools import (ARGUMENTS, POLICY, MAX_CALLS, tool_result_budget, bounded_result,
    clip, compact_json, redact_log, request_size, tool_definitions)
from app.models.chat_tools import INPUT_RESERVE
from app.models.research import RESEARCH_ARGUMENTS, RESEARCH_POLICY
from app.validators import validate_message_content
from app.models.training_task import TRAINING_ARGUMENTS,TRAINING_POLICY
from app.models.analysis_tools import ANALYSIS_ARGUMENTS,ANALYSIS_POLICY,analysis_projection
from app.models.analysis import AnalysisStorageError,AnalysisProtocolError
from app.models.event_bus import EventPayloadError
from app.models.source_code import SOURCE_ARGUMENTS,SOURCE_POLICY,SourceProtocolError,code_evidence,attach_code_sources
from app.runner.source_reader import SourceReadError
from app.models.suggestion_tools import SUGGESTION_ARGUMENTS,SUGGESTION_POLICY,suggestion_projection
from app.models.code_change_tools import READ_CHANGE_ARGUMENTS,WRITE_CHANGE_ARGUMENTS,CHANGE_POLICY,attach_changes
from app.models.revision_tools import REVISION_READ,REVISION_WRITE,REVISION_POLICY

from app.models.paper import PAPER_ARGUMENTS,PAPER_POLICY,evidence as paper_evidence,render_answer as render_paper_answer
from app.models.history_recall import HISTORY_ARGUMENTS,HISTORY_POLICY
from app.services.tool_protocol import ToolTextProtocolError,contains_tool_markup

logger=logging.getLogger(__name__)
UNAVAILABLE=(MySQLError,RunStorageError,MetricStorageError,RunnerNotStartedError,RunnerProtocolError,
    RunnerConflictError,ServerNotFoundError,ServerStorageError,ServerConnectionError,ServerConnectionTimeoutError,
    ServerAuthenticationError,ServerHostKeyError,ServerAddressNotAllowedError,ServerBusyError,ServerCredentialDecryptionError)


def _run_summary(run):
    return {'run_id':run.run_id,'experiment_id':run.experiment_id,'title':clip(run.experiment_title,200),
            'status':run.status,'kind':run.experiment_kind,'created_at':run.created_at.isoformat(),
            'updated_at':run.updated_at.isoformat(),
            'finished_at':run.finished_at.isoformat() if run.finished_at else None,'exit_code':run.exit_code,
            'error_code':run.error_code,'report_available':run.has_result,'result_version':run.result_version,
            'status_note':'数据库最近观测状态；本工具未自动刷新远端。'}


def _report_projection(report,args):
    source=report['data'];spec=source.get('evaluation')
    definitions={item['name']:item for item in spec['metrics']} if spec else {}
    datasets={item['id']:item for item in spec['datasets']} if spec else {}
    rows=[item for item in source['series'] if all(value is None or item.get(key)==value
          for key,value in (('dataset',args.dataset),('split',args.split),('metric_name',args.metric_name)))]
    selected=[]
    for item in rows[args.offset:args.offset+8]:
        definition=definitions.get(item['metric_name']);dataset=datasets.get(item.get('dataset','unspecified'))
        selected.append({**{key:item.get(key) for key in ('dataset','split','metric_name','points','first_value','last_value',
                  'min_value','max_value','last_step','observed_best_value','quality_flags')},
                  'unit':clip(definition['unit'],48) if definition else None,
                  'direction':definition['direction'] if definition else None,
                  'dataset_version':clip(dataset['version'],64) if dataset else None})
    return {'report_version':report['version'],'generated_at':report['generated_at'].isoformat(),
            'is_stale':report['is_stale'],'collection_complete':source['collection_complete'],
            'status':source['status'],'warnings':source['warnings'],
            'task_type':spec['task_type'] if spec else None,
            'protocol_id':spec['protocol_id'] if spec else None,
            'protocol_version':clip(spec['protocol_version'],64) if spec else None,
            'series':selected,'series_total':len(rows),'offset':args.offset,
            'next_offset':args.offset+len(selected) if args.offset+len(selected)<len(rows) else None,
            'legacy_report':source.get('schema_version')!=2,
            'notice':'来自已保存报告，可能晚于或早于当前运行状态；未自动采集或重建。'}


class ReadOnlyChatService:
    @staticmethod
    async def _invoke(gateway,messages,**options):
        response=await gateway.invoke(messages,**options)
        if isinstance(response,dict) and contains_tool_markup(response.get('content')):
            if response.get('tool_calls'):response={**response,'content':''}
            else:raise ToolTextProtocolError('模型将工具协议错误地输出为正文。')
        return response

    def __init__(self,repository,runs,*,metrics_factory,runner_factory,research_factory=None,training_factory=None,analysis_factory=None,source_factory=None,suggestion_factory=None,change_factory=None,revision_factory=None,paper_factory=None):
        self.repository,self.runs=repository,runs
        self.metrics_factory,self.runner_factory=metrics_factory,runner_factory
        self.research_factory=research_factory
        self.training_factory=training_factory
        self.analysis_factory=analysis_factory
        self.source_factory=source_factory
        self.suggestion_factory=suggestion_factory
        self.change_factory=change_factory
        self.revision_factory=revision_factory
        self.paper_factory=paper_factory

    @property
    def input_reserve(self):
        base = 9000 if self.training_factory else 8000 if self.research_factory else INPUT_RESERVE
        return base + 2000 + (1500 if self.analysis_factory else 0) + (3000 if self.source_factory else 0) + (1200 if self.suggestion_factory else 0) + (2500 if self.change_factory else 0) + (2500 if self.revision_factory else 0) + (3500 if self.paper_factory else 0)

    @logged_operation('read_only_chat_service.list_calls')
    async def list_calls(self,user_id,session_id,**options):
        return await asyncio.to_thread(self.repository.list_calls,user_id,session_id,**options)

    async def _execute(self,user_id,name,args,bound):
        """Return evidence plus requested binding change; no exception text is exposed to model."""
        target=None
        try:
            if name=='get_latest_training_run':
                run=await asyncio.to_thread(self.runs.latest_training_run,user_id,experiment_id=args.experiment_id)
                if run is None:return {'ok':True,'code':'NO_RUN','data':{}},'clear',None
                target=run.run_id
                return {'ok':True,'code':'OK','run_id':target,'data':_run_summary(run)},'set',target
            target=args.run_id or bound
            if target is None:return {'ok':False,'code':'NO_LINKED_RUN','data':{'hint':'先查询最近训练或指定本人的 run_id。'}},'keep',None
            # Ownership lookup precedes even construction of SSH or report collaborators.
            run=await asyncio.to_thread(self.runs.get_run,user_id,target)
            if name=='get_run_suggestions':
                if self.suggestion_factory is None:
                    return {'ok':False,'code':'UNSUPPORTED_TOOL'},'keep',None
                results=await self.suggestion_factory().results(user_id,target,limit=1)
                if not results:
                    return {'ok':True,'code':'SUGGESTIONS_NOT_READY','run_id':target,
                        'data':{'hint':'尚无已保存建议，未自动生成；请查看建议任务状态。'}},'set',target
                return {'ok':True,'code':'OK','run_id':target,'data':suggestion_projection(results[0],args)},'set',target
            if name in SOURCE_ARGUMENTS:
                if self.source_factory is None:
                    return {'ok':False,'code':'UNSUPPORTED_TOOL'},'keep',None
                action='list' if name=='list_project_files' else 'read'
                data=await self.source_factory().query(user_id,target,action,**args.model_dump(exclude={'run_id'},exclude_none=True))
                return {'ok':True,'code':'OK','run_id':target,'data':data},'set',target
            if name=='get_run_analysis':
                if self.analysis_factory is None:
                    return {'ok':False,'code':'UNSUPPORTED_TOOL'},'keep',None
                results=await self.analysis_factory().results(user_id,target,limit=1)
                if not results:
                    return {'ok':True,'code':'ANALYSIS_NOT_READY','run_id':target,
                            'data':{'hint':'尚无已保存分析；可能未启用、排队中、执行中或失败，未自动启动分析。'}},'set',target
                return {'ok':True,'code':'OK','run_id':target,'data':analysis_projection(results[0],args)},'set',target
            if name=='get_run_report':
                report=await self.metrics_factory().report(user_id,target)
                if report['data'].get('schema_version')!=2:
                    return {'ok':False,'code':'REPORT_REBUILD_REQUIRED','run_id':target,
                            'data':{'report_version':report['version'],'hint':'旧版报告可能混合数据集，请先通过报告接口重建；未自动重建。'}},'set',target
                data=_report_projection(report,args)
                data['current_run_status']=run.status
                data['current_run_updated_at']=run.updated_at.isoformat()
                return {'ok':True,'code':'OK','run_id':target,'data':data},'set',target
            runner=self.runner_factory()
            offset=args.offset
            if offset is None:
                head=await runner.operate(user_id,target,'logs',offset=0,limit=1)
                if head['observation']['status']=='missing':
                    return {'ok':False,'code':'LOG_UNAVAILABLE','run_id':target},'set',target
                offset=max(0,head['observation']['log_size']-args.limit)
            result=await runner.operate(user_id,target,'logs',offset=offset,limit=args.limit)
            observation=result['observation']
            if observation['status']=='missing':return {'ok':False,'code':'LOG_UNAVAILABLE','run_id':target},'set',target
            redacted=redact_log(observation['content'])
            data={'kind':'run_log','text':redacted,'offset':offset,
                  'next_offset':observation['next_offset'],'log_size':observation['log_size'],
                  'has_more':observation['has_more'],'display_truncated':False,
                  'is_complete_log':offset==0 and not observation['has_more'] and not observation['stale'] and not observation['truncated'] and not observation['source_changed'],
                  'status':observation['status'],'stale':observation['stale'],
                  'source_changed':observation['source_changed'],'log_truncated':observation['truncated'],
                  'notice':'这是读取时的日志窗口，已尽力脱敏；仅 is_complete_log=true 表示从开头读完本次可用日志，运行中仍可能追加。读取不会启动或停止训练。'}
            return {'ok':True,'code':'OK','run_id':target,'data':data},'set',target
        except (RunNotFoundError,ExperimentNotFoundError):
            return {'ok':False,'code':'NOT_FOUND','data':{'hint':'记录不存在或不可访问。'}},'keep',None
        except ReportNotFoundError:
            return {'ok':False,'code':'REPORT_NOT_GENERATED','run_id':target,'data':{'hint':'报告尚未生成，未自动生成或采集。'}},'set',target
        except RunnerNotStartedError:
            return {'ok':False,'code':'RUN_NOT_STARTED','run_id':target,'data':{'hint':'此运行未绑定远端执行，无法读取服务器日志或源码。'}},'set',target
        except SourceReadError as error:
            return {'ok':False,'code':'SOURCE_'+error.code,'run_id':target,
                    'data':{'hint':'源码未取得：可能路径受限、包含凭据、文件缺失、版本变化或超出读取限制；不能推测内容。'}},'set',target
        except (*UNAVAILABLE,AnalysisStorageError,AnalysisProtocolError,EventPayloadError,SourceProtocolError) as error:
            log_failure(logger,error,operation='read_only_chat_service.query',
                        level=logging.ERROR if isinstance(error,(MySQLError,RunStorageError,MetricStorageError,ServerStorageError,RunnerProtocolError,ServerCredentialDecryptionError)) else logging.WARNING)
            return {'ok':False,'code':'TEMPORARILY_UNAVAILABLE','data':{'hint':'当前暂时无法读取，请稍后重试。'}},'keep',None

    @logged_operation('read_only_chat_service.answer')
    async def answer(self,history,*,user_id,session_id,attempt,gateway,max_input_tokens,on_delta=None):
        try:
            async with asyncio.timeout(360 if self.change_factory else 240 if self.research_factory else 180):
                return await self._answer(history,user_id=user_id,session_id=session_id,attempt=attempt,
                                          gateway=gateway,max_input_tokens=max_input_tokens,on_delta=on_delta)
        except TimeoutError as error:
            raise ChatToolProtocolError('本次查询与回答超时，请稍后重试。') from error

    async def _answer(self,history,*,user_id,session_id,attempt,gateway,max_input_tokens,on_delta=None):
        bound=await asyncio.to_thread(self.repository.binding,user_id,session_id,attempt)
        changes=self.change_factory() if self.change_factory else None
        allow_changes=await changes.mode(user_id,session_id,attempt) if changes else False
        training=self.training_factory() if self.training_factory else None
        if training and not await training.mode(user_id,session_id,attempt):training=None
        revisions=self.revision_factory() if self.revision_factory else None
        allow_revisions=await revisions.mode(user_id,session_id,attempt) if revisions else False
        if allow_revisions:allow_changes=False
        if allow_changes or allow_revisions:training=None
        # 一轮处理训练操作或论文研究；避免把执行工具与不可信网页混在一次工具循环里。
        research=self.research_factory() if self.research_factory and not training and not allow_changes and not allow_revisions else None
        source_enabled=self.source_factory is not None and training is None and not allow_changes and not allow_revisions
        papers=self.paper_factory() if self.paper_factory and not training and not allow_changes and not allow_revisions else None
        max_calls=6 if research or training or self.analysis_factory or source_enabled or self.suggestion_factory or changes or revisions or papers else MAX_CALLS
        arguments_models={**ARGUMENTS,**HISTORY_ARGUMENTS,**(RESEARCH_ARGUMENTS if research else {}),**(TRAINING_ARGUMENTS if training else {}),
                          **(ANALYSIS_ARGUMENTS if self.analysis_factory else {}),**(SOURCE_ARGUMENTS if source_enabled else {}),
                          **(SUGGESTION_ARGUMENTS if self.suggestion_factory else {}),
                          **(PAPER_ARGUMENTS if papers else {}),**(READ_CHANGE_ARGUMENTS if changes else {}),**(REVISION_READ if revisions else {}),
                          **(REVISION_WRITE if allow_revisions else {}),**(WRITE_CHANGE_ARGUMENTS if allow_changes else {})}
        tools=tool_definitions(include_history=True,include_research=research is not None,include_training=training is not None,
                               include_analysis=self.analysis_factory is not None,include_source=source_enabled,
                               include_suggestions=self.suggestion_factory is not None,include_changes=changes is not None,allow_changes=allow_changes,include_revisions=revisions is not None,allow_revisions=allow_revisions,include_papers=papers is not None)
        delivered={};research_used=False
        paper_delivered={};paper_read_attempted=False;paper_search_attempted=False;paper_hits=[]
        messages=[{'role':{'human':'user','ai':'assistant'}.get(role,role),'content':text} for role,text in history]
        for message in messages:
            if message['role']=='assistant' and contains_tool_markup(message['content']):
                message['content']='此前助手响应格式异常，不代表其中提到的工具已经执行；请根据当前真实工具结果核实。'
        if not messages or messages[0]['role']!='system':raise ChatToolProtocolError('缺少受控系统消息。')
        policy=POLICY if research is None else POLICY.replace('或联网查论文','').replace('每轮最多四次工具调用','每轮最多六次工具调用')
        if training:
            policy=policy.replace('当前仅有查询能力，不能启动/停止训练、修改代码、同步指标、生成报告或联网查论文，不能声称已经执行。',
                '本轮启用训练目录与启动工具，不能修改代码、同步指标或生成报告。').replace('每轮最多四次工具调用','每轮最多六次工具调用')
            policy+='\n'+TRAINING_POLICY
        if allow_changes:
            policy=policy.replace('当前仅有查询能力，不能启动/停止训练、修改代码、同步指标、生成报告或联网查论文，不能声称已经执行。',
                '本轮允许按当前人类明确指令准备并应用一份修改到独立副本；不能覆盖原项目或启动训练。')
        if changes:policy=policy.replace('每轮最多四次工具调用','每轮最多六次工具调用')+'\n'+CHANGE_POLICY
        if allow_revisions:
            policy=policy.replace('当前仅有查询能力，不能启动/停止训练、修改代码、同步指标、生成报告或联网查论文，不能声称已经执行。',
                '本轮可按明确原文启动已准备的修改副本训练，不能应用新代码修改或执行任意命令。')
        if revisions:policy+='\n'+REVISION_POLICY
        messages[0]['content']+='\n'+policy+'\n会话关联 run_id：'+(bound or '无')
        if self.analysis_factory:
            messages[0]['content']=messages[0]['content'].replace('每轮最多四次工具调用','每轮最多六次工具调用')+'\n'+ANALYSIS_POLICY
        if self.suggestion_factory:
            messages[0]['content']=messages[0]['content'].replace('每轮最多四次工具调用','每轮最多六次工具调用')+'\n'+SUGGESTION_POLICY
        if source_enabled:
            messages[0]['content']=messages[0]['content'].replace('每轮最多四次工具调用','每轮最多六次工具调用')+'\n'+SOURCE_POLICY
        if research:
            messages[0]['content']+='\n'+RESEARCH_POLICY
            hints=await research.hints(user_id,session_id)
            messages[0]['content']+='\n本会话最近资料元信息（不等于本轮已读，引用前重新 read_reference）：'+compact_json(hints)
        if papers:
            messages[0]['content']+='\n'+PAPER_POLICY
        messages[0]['content']+='\n'+HISTORY_POLICY
        calls_used,result_bytes,seen=0,0,set()
        protocol_repaired=False
        source_evidence=[]
        source_versions={}
        source_read_attempted=False
        change_apply_attempted=False
        change_prepare_attempted=False
        prepared_revisions=[]
        revision_starts=[]
        revision_start_attempted=False
        revision_prepare_attempted=False
        prepared_changes=[]
        change_results=[]
        result_budget=tool_result_budget(max_input_tokens,include_papers=papers is not None)
        for _ in range(max_calls+1):
            if request_size(messages,tools)>max_input_tokens:
                raise ChatToolBudgetError('工具上下文超过本次输入预算，请缩小查询范围。')
            remaining=min(result_budget-result_bytes,max_input_tokens-request_size(messages,tools)-512)
            if on_delta is not None:await on_delta(None)
            choice='none' if calls_used>=max_calls or remaining<512 else 'auto'
            options={'tools':tools,'tool_choice':choice,**({'on_delta':on_delta} if on_delta is not None else {})}
            try:
                response=await self._invoke(gateway,messages,**options)
            except ToolTextProtocolError:
                if protocol_repaired:raise
                protocol_repaired=True
                logger.warning('模型工具响应格式异常，正在纠正一次；未从正文执行工具')
                if on_delta is not None:await on_delta(None)
                messages[0]['content']+='\n上次响应格式异常。不要在正文输出工具协议标记。'+(
                    '本轮工具额度已用尽，只能依据已取得证据用自然语言回答，明确未读范围；不要申请用户再授权已要求的只读查询。'
                    if choice=='none' else '需要查询时请使用 API 正式的 tool_calls，最终正文只写给用户的自然语言。')
                if request_size(messages,tools)>max_input_tokens:raise ChatToolBudgetError('工具上下文超过本次预算。')
                response=await self._invoke(gateway,messages,**options)
            if type(response) is not dict:raise ChatToolProtocolError('模型工具响应格式不符合要求。')
            calls=response.get('tool_calls',[])
            if type(calls) is not list:raise ChatToolProtocolError('工具调用列表不符合要求。')
            if not calls:
                try:
                    content=validate_message_content(response.get('content'))
                    if source_read_attempted and not source_evidence:
                        return '本轮没有取得可引用的源码片段，可能读取失败、文件为空或窗口超出文件范围；暂不能确认具体代码实现。'
                    if paper_delivered:answer=render_paper_answer(content,paper_delivered,delivered)
                    elif paper_read_attempted:return '本轮没有读取到可引用的论文正文；请检查本人论文的索引状态、片段编号或检索关键词，不能由模型补造依据。'
                    elif paper_search_attempted and not delivered:
                        from app.models.paper import render_search_hits
                        answer=render_search_hits(paper_hits)
                    else:answer=research.finish(content,delivered,bound) if research_used else content
                    if change_apply_attempted and not change_results:
                        return '本轮没有取得已保存的代码修改结果，请查看同一修改记录的状态后重试；未启动训练。'
                    if change_prepare_attempted and not change_apply_attempted:
                        return ('已准备修改计划，但尚未应用到服务器独立副本。修改 ID：'+', '.join(prepared_changes)
                            if prepared_changes else '本轮未能取得准备完成的修改计划，请检查指定建议、运行归属或当前源码；未执行代码修改。')
                    if revision_start_attempted:
                        if revision_starts:
                            return '已提交修改副本训练请求：'+', '.join(revision_starts)+'。请查询真实运行状态；启动请求不代表训练已完成。'
                        return '未能确认启动结果；请查询本轮准备或指定的原 run_id，仅重试同一运行，不新建运行重跑。'
                    if revision_prepare_attempted:
                        return ('新运行计划已保存，尚未启动训练：'+', '.join(prepared_revisions)) if prepared_revisions else '本轮未准备完成修改后训练，请核对修改记录、部署入口和文件版本。'
                    return attach_changes(attach_code_sources(answer,source_evidence),change_results)
                except ValueError as error:
                    raise ChatToolProtocolError('模型没有返回有效的回答正文。') from error
            if not 1<=len(calls)<=max_calls-calls_used:
                raise ChatToolProtocolError('模型超过本轮工具调用次数限制。')
            if on_delta is not None:await on_delta(None)
            normalized=[]
            for call in calls:
                if (type(call) is not dict or not isinstance(call.get('id'),str)
                    or not re.fullmatch('[A-Za-z0-9_-]{1,128}',call['id']) or call['id'] in seen
                    or not isinstance(call.get('name'),str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',call['name'])
                    or type(call.get('args')) is not dict):
                    raise ChatToolProtocolError('模型工具调用标识或参数格式不符合要求。')
                seen.add(call['id'])
                try:
                    if len(compact_json(call['args']).encode())>2048:raise ValueError('argument budget')
                except (ValueError,TypeError,RecursionError,UnicodeError) as error:
                    raise ChatToolProtocolError('工具参数格式或预算不符合要求。') from error
                normalized.append({'id':call['id'],'type':'function','function':{'name':call['name'],'arguments':compact_json(call['args'])}})
            if response.get('content') is not None and not isinstance(response['content'],str):
                raise ChatToolProtocolError('工具调用正文必须是文本。')
            messages.append({'role':'assistant','content':clip(response.get('content') or '',1024),'tool_calls':normalized})
            for call in calls:
                calls_used+=1
                started=perf_counter();arguments={};name=call['name'];binding='keep';target=None;pending_references=[]
                await asyncio.to_thread(self.repository.check_attempt,user_id,session_id,attempt)
                if papers is not None and name=='search_papers':paper_search_attempted=True
                if papers is not None and name=='read_paper_chunk':paper_read_attempted=True
                if research is not None and name in RESEARCH_ARGUMENTS:research_used=True
                if source_enabled and name=='read_project_file':source_read_attempted=True
                if allow_revisions and name=='start_revision_run':revision_start_attempted=True
                if allow_revisions and name=='prepare_revision_run':revision_prepare_attempted=True
                if allow_changes and name=='apply_code_change':change_apply_attempted=True
                if allow_changes and name=='prepare_code_change':change_prepare_attempted=True
                if name not in arguments_models:
                    result={'ok':False,'code':'UNSUPPORTED_TOOL'};name='unsupported'
                else:
                    try:
                        args=arguments_models[name].model_validate(call['args'])
                        arguments=args.model_dump(exclude_none=True,mode='json')
                    except ValueError:
                        result={'ok':False,'code':'INVALID_ARGUMENTS'}
                    else:
                        try:
                            is_research=research is not None and name in RESEARCH_ARGUMENTS
                            available=min(result_budget-result_bytes,max_input_tokens-request_size(messages,tools)-512)
                            async with asyncio.timeout(160 if name=='prepare_code_change' else 50 if name=='prepare_revision_run' else 125 if name=='apply_code_change' else 45 if is_research else 30):
                                minimum=(3500 if name in SOURCE_ARGUMENTS or name == 'read_training_project_file' else 1800
                                         if (training is not None and name in {'prepare_training_run','start_training_run'}) or name in {'prepare_revision_run','start_revision_run'} else 512)
                                if available<minimum:
                                    result={'ok':False,'code':'RESULT_BUDGET'}
                                elif name in HISTORY_ARGUMENTS:
                                    result=await asyncio.to_thread(self.repository.recall_history,user_id,session_id,attempt,name,args)
                                elif papers is not None and name in PAPER_ARGUMENTS:
                                    result=await papers.tool(user_id,name,args)
                                elif revisions is not None and name in {**REVISION_READ,**REVISION_WRITE}:
                                    result=await revisions.execute(user_id,session_id,attempt,name,args,bound)
                                    if result.get('run_id'):binding,target='set',result['run_id']
                                elif changes is not None and name in {**READ_CHANGE_ARGUMENTS,**WRITE_CHANGE_ARGUMENTS}:
                                    result=await changes.execute(user_id,session_id,attempt,name,args,bound)
                                    if result.get('run_id'):binding,target='set',result['run_id']
                                elif training is not None and name in TRAINING_ARGUMENTS:
                                    result=await training.execute(user_id,session_id,attempt,name,args)
                                    if result.get('run_id'):binding,target='set',result['run_id']
                                elif is_research:
                                    result,pending_references=await research.execute(user_id,session_id,attempt,name,args,bound)
                                else:
                                    if name=='read_project_file' and args.expected_sha256 is None:
                                        known=source_versions.get((args.run_id or bound,args.path))
                                        if known is not None:args=args.model_copy(update={'expected_sha256':known})
                                        arguments=args.model_dump(exclude_none=True,mode='json')
                                    result,binding,target=await self._execute(user_id,name,args,bound)
                        except TimeoutError:
                            result={'ok':False,'code':'TOOL_TIMEOUT'}
                if name=='read_project_file' and result.get('ok') and result.get('data',{}).get('lines'):
                    current=result['data']
                    previous=source_versions.get((result['run_id'],current['path']))
                    if previous is not None and previous!=current['file_sha256']:
                        result={'ok':False,'code':'SOURCE_SOURCE_CHANGED','run_id':result['run_id'],
                                'data':{'hint':'本轮文件版本发生变化，请在新的提问中重新检查；未混合新旧片段。'}}
                    else:
                        current['citation_id']='code_'+str(len(source_evidence)+1)
                allowance=min(result_budget-result_bytes,max_input_tokens-request_size(messages,tools)-512)
                result=bounded_result(result,max(0,allowance))
                text=compact_json(result);result_bytes+=len(text.encode())
                metadata={'code':result['code'],'run_id':result.get('run_id'),'result_bytes':len(text.encode()),
                          'plan_id':result.get('data',{}).get('plan_id'),
                          'change_id':result.get('data',{}).get('change_id'),
                          'requested_tool':call['name'],
                          'truncated':result.get('truncated',False),'report_version':result.get('data',{}).get('report_version'),
                          'quality':{key:result.get('data',{}).get(key) for key in
                                     ('is_stale','collection_complete','source_changed','display_truncated')}}
                evidence=code_evidence(result) if name=='read_project_file' else None
                pdf_evidence=paper_evidence(result) if name=='read_paper_chunk' else None
                if pdf_evidence is not None:metadata['paper_evidence']=pdf_evidence
                if evidence is not None:metadata['code_evidence']=evidence
                # Audit and binding commit together, only if this attempt is still live.
                await asyncio.to_thread(self.repository.record,user_id,session_id,attempt,call_id=call['id'],
                    call_index=calls_used,name=name,arguments=arguments,outcome=result['code'],metadata=metadata,
                    duration_ms=min(3600000,int((perf_counter()-started)*1000)),binding=binding,run_id=target,
                    **({'reference_rows':pending_references} if pending_references else {}))
                if name=='search_papers' and result.get('ok'):paper_hits.extend(result['data'].get('items',[]))
                if pdf_evidence is not None:
                    paper_delivered.setdefault(pdf_evidence['chunk_id'],[]).append(pdf_evidence)
                    # 本轮已含私人 PDF 正文，后续模型调用不再具备公开外发工具。
                    tools=[item for item in tools if item['function']['name'] not in RESEARCH_ARGUMENTS]
                    for public_name in RESEARCH_ARGUMENTS:arguments_models.pop(public_name,None)
                if evidence is not None:
                    source_evidence.append(evidence)
                    source_versions[(target,evidence['path'])]=evidence['file_sha256']
                    if args.path is None:source_versions[(target,None)]=evidence['file_sha256']
                if name=='prepare_revision_run' and result.get('ok'):prepared_revisions.append(result['run_id'])
                if name=='start_revision_run' and result.get('ok'):revision_starts.append(result['run_id']+'（'+result['data']['status']+'）')
                if name=='apply_code_change' and result.get('ok') and result.get('data',{}).get('status') in {'applied','checks_failed'}:
                    change_results.append(result['data'])
                if name=='prepare_code_change' and result.get('ok') and result.get('data',{}).get('status')=='ready':
                    prepared_changes.append(result['data']['change_id'])
                if research and name in RESEARCH_ARGUMENTS:research.remember(result,delivered)
                if binding=='set':bound=target
                elif binding=='clear':bound=None
                messages.append({'role':'tool','tool_call_id':call['id'],'content':text})
        raise ChatToolProtocolError('模型未在查询预算内完成回答。')
