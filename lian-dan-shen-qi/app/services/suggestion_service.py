"""独立建议 Agent：冻结分析输入，有界只读工具，先保存工具证据再交给模型。"""
import asyncio
from datetime import datetime, timezone
import logging
import re

from app.errors import ModelCredentialConfigurationError
from app.logging_config import log_failure
from app.models.analysis import AnalysisProtocolError, compact
from app.models.chat_tools import bounded_result, request_size, clip
from app.models.research import (RESEARCH_ARGUMENTS, RESEARCH_DESCRIPTIONS, ResearchUnavailableError)
from app.models.source_code import SOURCE_ARGUMENTS, SOURCE_DESCRIPTIONS
from app.models.suggestion import (SuggestionSettings, CodeEvidence, PaperEvidence, PdfPaperEvidence, POLICY,
    analysis_input, render_suggestions)
from app.runner.source_reader import SourceReadError
from app.services.analysis_service import AnalysisService
from app.services.tool_protocol import ToolTextProtocolError
from app.services.read_only_chat_service import UNAVAILABLE
from app.models.source_code import SourceProtocolError

from app.models.paper import PAPER_ARGUMENTS,PAPER_DESCRIPTIONS

logger = logging.getLogger(__name__)
ARGUMENTS = {**SOURCE_ARGUMENTS, **RESEARCH_ARGUMENTS}
DESCRIPTIONS = {**SOURCE_DESCRIPTIONS, **RESEARCH_DESCRIPTIONS}


def tools(include_papers=False):
    models={**ARGUMENTS,**(PAPER_ARGUMENTS if include_papers else {})}
    descriptions={**DESCRIPTIONS,**(PAPER_DESCRIPTIONS if include_papers else {})}
    return [{'type': 'function', 'function': {'name': name, 'description': descriptions[name],
        'parameters': model.model_json_schema()}} for name, model in models.items()]


class SuggestionService(AnalysisService):
    stage_name = 'suggestion'
    def __init__(self, repository, *, gateway_factory=None, source_factory=None, research_factory=None, paper_factory=None, settings=None):
        super().__init__(repository, gateway_factory=gateway_factory, settings=settings or SuggestionSettings())
        self.source_factory, self.research_factory = source_factory, research_factory
        self.paper_factory=paper_factory

    async def evidence(self, user_id, run_id, task_id):
        return await asyncio.to_thread(self.repository.evidence, user_id, run_id, task_id)

    async def _tool(self, lease, name, args, references, versions):
        try:
            if name in PAPER_ARGUMENTS:
                return await self.paper_factory().tool(lease['user_id'],name,args) if self.paper_factory else {'ok':False,'code':'PAPER_UNAVAILABLE'}
            if name in SOURCE_ARGUMENTS:
                if self.source_factory is None:
                    return {'ok': False, 'code': 'SOURCE_UNAVAILABLE'}
                if args.run_id not in (None, lease['run_id']):
                    return {'ok': False, 'code': 'INVALID_ARGUMENTS'}
                options = args.model_dump(exclude={'run_id'}, exclude_none=True)
                if name == 'read_project_file' and args.path in versions and not args.expected_sha256:
                    options['expected_sha256'] = versions[args.path]
                data = await self.source_factory().query(lease['user_id'], lease['run_id'],
                    'list' if name == 'list_project_files' else 'read', **options)
                if name == 'read_project_file':
                    if data['path'] in versions and versions[data['path']] != data['file_sha256']:
                        return {'ok': False, 'code': 'SOURCE_CHANGED'}
                    versions[data['path']] = data['file_sha256']
                    if args.path is None:
                        versions[None] = data['file_sha256']
                return {'ok': True, 'code': 'OK', 'data': data}
            if self.research_factory is None:
                return {'ok': False, 'code': 'RESEARCH_DISABLED'}
            client = self.research_factory()
            if name == 'search_references':
                rows = await client.search(args.query, args.limit, lease['run_id'])
                references.update({row['reference_id']: row for row in rows})
                return {'ok': True, 'code': 'OK' if rows else 'NO_REFERENCES', 'data': {'items': [
                    {key: row[key] for key in ('reference_id', 'url', 'title', 'kind')} for row in rows], 'abstract_only': True}}
            row = references.get(args.reference_id) if args.reference_id else None
            if args.reference_id and row is None:
                return {'ok': False, 'code': 'REFERENCE_NOT_FOUND'}
            if row is None:
                row = await client.read(args.url, lease['run_id'])
            elif row['kind'] == 'search_abstract' and args.offset == 0:
                try:
                    row = await client.read(row['url'].replace('/abs/', '/html/'), lease['run_id'])
                except ResearchUnavailableError:
                    pass
            references[row['reference_id']] = row
            text = clip(row['content'][args.offset:], args.max_bytes)
            end = args.offset + len(text)
            return {'ok': True, 'code': 'OK', 'data': {
                'reference_id': row['reference_id'], 'url': row['url'], 'title': row['title'], 'kind': row['kind'],
                'content_sha256': row['content_sha256'], 'offset': args.offset, 'text': text,
                'next_offset': end if end < len(row['content']) else None, 'content_length': len(row['content']),
                'abstract_only': row['kind'] in {'search_abstract', 'paper_abstract'},
                'retrieved_at': datetime.now(timezone.utc).isoformat()}}
        except SourceReadError as error:
            return {'ok': False, 'code': 'SOURCE_' + error.code}
        except (*UNAVAILABLE, SourceProtocolError, ResearchUnavailableError, TimeoutError) as error:
            log_failure(logger, error, operation='suggestion_service.tool', level=logging.WARNING)
            return {'ok': False, 'code': 'READ_UNAVAILABLE'}

    async def _generate(self, lease, source, gateway):
        analysis = analysis_input(source)
        policy=POLICY
        if self.paper_factory:
            policy+='\n可以使用 list_papers/search_papers/read_paper_chunk 查本人上传的论文；先读正文再引用。每块最多6000字符，默认读取12000字节；next_offset非空要继续读，模块跨块或跨页时用同一paper_id和previous_chunk_index/next_chunk_index继续读取，核对解决的问题、动机和方法，不能以一句话冒充完整方法。uploaded_pdf 是带物理页码的正文证据，使用其实际 paper_N 引文；不发送私人论文片段到公开搜索。读取私人 PDF 后本轮会关闭公开网络工具，需要公开资料时先读公开来源，再读 PDF。最终仍使用原有 suggestions JSON 格式。'
        messages = [{'role': 'system', 'content': policy}, {'role': 'user', 'content': compact({
            'run_id': lease['run_id'], 'analysis': analysis, 'max_tool_calls': self.settings.max_calls})}]
        definitions = tools(include_papers=self.paper_factory is not None)
        models={**ARGUMENTS,**(PAPER_ARGUMENTS if self.paper_factory else {})}
        codes, papers, references, versions, seen = [], [], {}, {}, set()
        count = 0
        # 先实际读取主网络，不能用训练入口冒充模型实现；占用同一工具额度并保存审计证据。
        if self.source_factory is not None:
            source_service=self.source_factory()
            if hasattr(source_service,'main_network'):
                network=await source_service.main_network(lease['user_id'],lease['run_id'])
                if isinstance(network,dict) and network.get('path'):
                    options={'path':network['path'],'line_count':60}
                    if network.get('file_sha256'):options['expected_sha256']=network['file_sha256']
                    args=SOURCE_ARGUMENTS['read_project_file'](**options)
                    result=await self._tool(lease,'read_project_file',args,references,versions)
                    count=1
                    seen.add('selected_main_network')
                    if result.get('ok'):result['data']['evidence_id']='code_1'
                    result=bounded_result(result,8192)
                    await asyncio.to_thread(self.repository.record,lease['user_id'],lease['run_id'],lease['task_id'],lease['lease_token'],count,'read_project_file',options,result)
                    if result.get('ok') and result.get('data',{}).get('lines'):
                        codes.append({key:result['data'][key] for key in CodeEvidence.model_fields})
                    messages[0]['content']+='\n用户指定的主网络为 '+network['path']+'。必须从该文件检查模型实现；首段已读取，按 next_line 和文件哈希继续读取关键 forward 等实现和必要依赖。首段可能只有导入，不得把导入段当作完整网络。'
                    messages.extend([{'role':'assistant','content':None,'tool_calls':[{'id':'selected_main_network','type':'function','function':{'name':'read_project_file','arguments':compact(options)}}]},
                                     {'role':'tool','tool_call_id':'selected_main_network','content':compact(result)}])
        protocol_repaired = False
        for _ in range(self.settings.max_calls + 1):
            await self._renew(lease)
            remaining = self.settings.max_input_tokens - request_size(messages, definitions)
            if remaining < 1024:
                return render_suggestions(compact({'suggestions': [], 'limitations': ['本轮证据预算不足，需缩小检查范围。']}), source, codes, papers)
            choice='none' if count >= self.settings.max_calls or remaining < 4096 else 'auto'
            if choice=='none' and not codes:
                return render_suggestions(compact({'suggestions':[],
                    'limitations':['本轮查询额度或上下文已用尽，未同时取得可引用源码和论文/官方资料正文；暂不形成修改建议。']}),source,codes,papers)
            options={'max_tokens':self.settings.max_output_tokens}
            if choice=='none':options['json_mode']=True
            else:options.update(tools=definitions,tool_choice=choice)
            try:
                async with asyncio.timeout(self.settings.model_timeout_seconds):
                    response = await gateway.invoke(messages,**options)
            except ToolTextProtocolError:
                if protocol_repaired:raise
                protocol_repaired=True
                messages[0]['content']+='\n上次响应把工具协议输出在正文中。正文不得包含 DSML 等工具标记。'+(
                    '工具额度已用尽，请根据已取得证据返回规定的最终 JSON；证据不足则返回空建议并说明缺口。'
                    if choice=='none' else '需要查询时必须使用 API 原生 tool_calls，最终正文只返回规定的 JSON。')
                if self.settings.max_input_tokens-request_size(messages,definitions)<1024:
                    raise AnalysisProtocolError('建议格式纠正超出输入预算。')
                await self._renew(lease)
                async with asyncio.timeout(self.settings.model_timeout_seconds):
                    response=await gateway.invoke(messages,**options)
            if type(response) is not dict or type(response.get('tool_calls', [])) is not list:
                raise AnalysisProtocolError('建议模型响应结构错误。')
            calls = response.get('tool_calls', [])
            if not calls:
                if not codes:
                    # 失败/摘要/仅目录不能由模型补造代码或正文依据。
                    text = compact({'suggestions': [], 'limitations': ['未同时取得可引用源码和论文/官方资料正文，暂不形成修改建议；请查看工具证据中的缺失项。']})
                else:
                    text = response.get('content')
                return render_suggestions(text, source, codes, papers)
            if not 1 <= len(calls) <= self.settings.max_calls - count:
                raise AnalysisProtocolError('建议模型超出工具调用次数。')
            normalized = []
            for call in calls:
                if (type(call) is not dict or type(call.get('args')) is not dict
                    or type(call.get('name')) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', call['name'])
                    or type(call.get('id')) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', call['id'])
                    or call['id'] in seen or len(compact(call['args']).encode()) > 2048):
                    raise AnalysisProtocolError('建议工具参数或标识错误。')
                seen.add(call['id'])
                normalized.append({'id': call['id'], 'type': 'function', 'function': {'name': call['name'], 'arguments': compact(call['args'])}})
            messages.append({'role': 'assistant', 'content': None, 'tool_calls': normalized})
            for call in calls:
                count += 1
                await self._renew(lease)
                name, arguments = call['name'], {}
                available = self.settings.max_input_tokens - request_size(messages, definitions) - 512
                if name not in models:
                    name, result = 'unsupported', {'ok': False, 'code': 'UNSUPPORTED_TOOL'}
                else:
                    try:
                        args = models[name].model_validate(call['args'])
                        arguments = args.model_dump(exclude_none=True, mode='json')
                    except ValueError:
                        result = {'ok': False, 'code': 'INVALID_ARGUMENTS'}
                    else:
                        if available < 4096:
                            result = {'ok': False, 'code': 'RESULT_BUDGET'}
                        else:
                            try:
                                async with asyncio.timeout(40):
                                    result = await self._tool(lease, name, args, references, versions)
                            except TimeoutError:
                                result = {'ok': False, 'code': 'READ_TIMEOUT'}
                if result.get('ok') and name in {'read_project_file', 'read_reference','read_paper_chunk'}:
                    result['data']['evidence_id'] = ('code_' if name == 'read_project_file' else 'paper_') + str(count)
                ceiling=16384 if result.get('data',{}).get('kind')=='uploaded_pdf' else 8192
                result = bounded_result(result, min(ceiling, max(256, available)))
                # 所有网络完成后才开启短事务；提交失败不会把未经记录的资料交给模型。
                await asyncio.to_thread(self.repository.record, lease['user_id'], lease['run_id'], lease['task_id'],
                    lease['lease_token'], count, name, arguments, result)
                if result.get('ok'):
                    data = result.get('data', {})
                    if name == 'read_project_file' and data.get('lines'):
                        codes.append({key: data[key] for key in CodeEvidence.model_fields})
                    if name in {'read_reference','read_paper_chunk'} and data.get('text'):
                        model=PdfPaperEvidence if name=='read_paper_chunk' else PaperEvidence
                        papers.append({key:data[key] for key in model.model_fields})
                        if name=='read_paper_chunk':
                            definitions=[item for item in definitions if item['function']['name'] not in RESEARCH_ARGUMENTS]
                            for public_name in RESEARCH_ARGUMENTS:models.pop(public_name,None)
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': compact(result)})
        raise AnalysisProtocolError('建议模型没有完成最终回答。')

    async def _perform(self, lease):
        source = await asyncio.to_thread(self.repository.source, lease['user_id'], lease['task_id'], lease['lease_token'])
        if self.gateway_factory is None:
            raise ModelCredentialConfigurationError('尚未组装建议网关。')
        gateway = await self.gateway_factory.create_for_user(lease['user_id'])
        try:
            content = await self._generate(lease, source, gateway)
        finally:
            try:
                async with asyncio.timeout(5):
                    await gateway.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_failure(logger, error, operation='suggestion_service.gateway_cleanup')
        await asyncio.to_thread(self.repository.complete, lease['user_id'], lease['run_id'], lease['task_id'],
            lease['lease_token'], content, self.settings.model_name)
