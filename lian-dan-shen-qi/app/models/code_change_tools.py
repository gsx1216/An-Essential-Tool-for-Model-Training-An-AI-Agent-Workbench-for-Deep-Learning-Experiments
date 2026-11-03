"""聊天修改工具：只读查询常驻，准备/应用仅在当前明确修改模式提供。"""
from dataclasses import replace
from typing import Annotated
from uuid import UUID
from pydantic import Field

from app.models.chat_tools import RunArguments, Arguments, clip
from app.models.research import ResearchReply


class PrepareCodeChange(RunArguments):
    suggestion_task_id: UUID
    suggestion_index: Annotated[int, Field(strict=True, ge=1, le=3)]


class ChangeID(Arguments):
    change_id: UUID


READ_CHANGE_ARGUMENTS = {'list_code_changes': RunArguments, 'get_code_change': ChangeID}
WRITE_CHANGE_ARGUMENTS = {'prepare_code_change': PrepareCodeChange, 'apply_code_change': ChangeID}
CHANGE_ARGUMENTS = {**READ_CHANGE_ARGUMENTS, **WRITE_CHANGE_ARGUMENTS}
CHANGE_DESCRIPTIONS = {
    'list_code_changes': 'List this owned/session-linked run code modification records. Read-only, no model or SSH side effects.',
    'get_code_change': 'Read an owned stored modification, actual diff preview and checks. Never apply or train.',
    'prepare_code_change': 'Prepare an exact edit plan for one existing owned suggestion task and 1-based suggestion index. Requires explicit current human apply instruction. Does not write remote files.',
    'apply_code_change': 'Apply the owned prepared change into code_changes/<id>/project only on current explicit human instruction. Same ID reuses result. Never overwrite original project or start training.'}
CHANGE_POLICY = '''查询修改记录使用 list_code_changes/get_code_change，空记录不代表已经改过。
需要应用建议时先确认本人运行、已保存的 suggestion task_id 和第几条建议，不能猜编号。只有当前用户原文明确说“应用第1条建议”等指令，才能 prepare_code_change，再 apply_code_change；已有明确计划可直接指定 change_id 应用。
一次提问最多应用一份修改，不批量应用。计划准备好不等于代码已修改；applied 只表示独立副本已写入，checks_failed/incomplete 不能称为测试全部通过，更不能声称已经训练。按工具结果提供 change_id、独立目录、实际文件和检查状态。代码修改工具本身不覆盖原项目或启动训练；另有修改后训练工具时，必须走其独立授权和启动流程。'''


def projection(record):
    plan, result = record['plan'], record['result']
    data = {key: record[key] for key in ('change_id','run_id','suggestion_task_id','suggestion_index','status','error_code','lease_active')}
    if plan:
        data['explanation'] = clip(plan['explanation'], 400)
        data['planned_files'] = plan['files']
    if result:
        data.update(workspace_directory=result['workspace_directory'], check_status=result['check_status'], notice=result['notice'])
        data['files'] = [{**{key: row[key] for key in ('path','before_sha256','after_sha256')},
            'diff_preview': clip(row['diff'], 1200), 'diff_truncated': len(row['diff'].encode())>1200} for row in result['files']]
        data['checks'] = [{**{key: row[key] for key in ('kind','name','status')},'detail': clip(row['detail'],180)} for row in result['checks'][:4]]
        data['checks_display_truncated'] = len(result['checks'])>4
    return data


def attach_changes(answer, records):
    if not records: return answer
    lines = ['\n\n代码修改记录（独立副本）']
    for row in records:
        lines += ['- 修改 ID：'+row['change_id'], '- 状态：'+row['status']+'；检查：'+row['check_status'],
            '- 目录：'+row['workspace_directory'], '- 文件：'+', '.join(file['path'] for file in row['files'])]
    lines += ['- 未覆盖原项目，未启动训练；完整差异和检查结果可通过修改记录接口查看。']
    text = '\n'.join(lines)
    return replace(answer, text=answer.text+text) if isinstance(answer, ResearchReply) else answer+text
