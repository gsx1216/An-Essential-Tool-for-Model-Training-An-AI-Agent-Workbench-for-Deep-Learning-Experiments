"""聊天读取已保存建议；不触发生成，片段与来源继续按原版本展示。"""
from typing import Annotated, Literal
from pydantic import Field
from app.models.chat_tools import RunArguments, clip


class SuggestionArguments(RunArguments):
    section: Literal['overview', 'suggestions', 'code_findings', 'advice'] = 'overview'
    offset: Annotated[int, Field(strict=True, ge=0, le=119)] = 0


SUGGESTION_ARGUMENTS = {'get_run_suggestions': SuggestionArguments}
SUGGESTION_DESCRIPTIONS = {'get_run_suggestions': 'Read an owned run saved code-and-paper suggestions. Overview, code_findings or one suggestion per page. code_findings contains source-backed model diagnosis without requiring papers. For direct text advice use section=advice and follow next_offset (500 characters per page). Never generate, modify code, or train. Preserve stale flag and evidence limitations.'}
SUGGESTION_POLICY = '查询已经生成的修改建议时使用 get_run_suggestions。空结果不等于已生成；is_stale=true 要说明属于旧报告。保存的源码是当时读取片段，不保证文件现在未变；论文引文存在不等于原因或效果已证实。工具只展示，不触发建议生成、代码修改或训练。'


def suggestion_projection(result, args):
    content = result['content']
    data = {'task_id': result['task_id'], 'report_version': result['report_version'], 'is_stale': result['is_stale'],
        'evidence_status': content['evidence_status'], 'notice': content['notice'],
        'limitations': [clip(text, 240) for text in content['limitations'][:3]], 'section': args.section}
    if content.get('direct_advice'):
        if args.section=='overview':
            data.update(suggestion_count=0,code_finding_count=0,has_direct_advice=True)
        else:
            text=content['direct_advice'];start=args.offset*500
            data.update(text=text[start:start+500],offset=args.offset,has_more=start+500<len(text),next_offset=args.offset+1 if start+500<len(text) else None,display_is_preview=True)
        return data
    items = content.get('code_findings',[]) if args.section=='code_findings' else content['suggestions']
    if args.section == 'overview':
        data['suggestion_count'] = len(items)
        data['code_finding_count']=len(content.get('code_findings',[]))
        data['analysis_task_id'] = content['source']['analysis_task_id']
        return data
    codes = {v['evidence_id']: v for v in content['code_evidence']}
    papers = {v['evidence_id']: v for v in content['paper_evidence']}
    selected = []
    for row in items[args.offset:args.offset+1]:
        value = {key: clip(row[key], 280) for key in ('problem', 'hypothesis', 'change', 'conditions', 'validation')}
        value['display_is_preview'] = True
        value['evidence_paths'] = row['evidence_paths']
        code = row['code'][0]
        source = codes[code['evidence_id']]
        value['code'] = {**code, 'path': source['path'], 'file_sha256': source['file_sha256'], 'retrieved_at': source['retrieved_at']}
        value['code_citation_count']=len(row['code'])
        if row.get('papers'):
            paper=row['papers'][0]
            source = papers[paper['evidence_id']]
            value['paper'] = {**paper, 'url': source['url'], 'kind': source['kind'], 'content_sha256': source['content_sha256']}
            if source['kind']=='uploaded_pdf':
                value['paper'].update({key:source[key] for key in ('paper_id','chunk_id','page_number','page_char_start','offset','file_sha256')})
            value['paper_citation_count']=len(row['papers'])
        selected.append(value)
    data.update({'items': selected, 'offset': args.offset, 'total': len(items),
                 'next_offset': args.offset+len(selected) if args.offset+len(selected)<len(items) else None})
    return data
