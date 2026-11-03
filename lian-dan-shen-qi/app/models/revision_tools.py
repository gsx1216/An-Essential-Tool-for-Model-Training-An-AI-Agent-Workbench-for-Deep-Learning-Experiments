"""修改后训练工具参数；模型不能指定服务器、源码路径或执行命令。"""
from uuid import UUID
from pydantic import Field, field_validator
from app.models.chat_tools import Arguments
from app.models.training_task import TaskRequest


class Change(Arguments):
    change_id:UUID


class Run(Arguments):
    run_id:UUID


class Prepare(Change):
    parameters:dict=Field(default_factory=dict)

    @field_validator('parameters')
    @classmethod
    def parameters_budget(cls,value):return TaskRequest.parameters(value)


REVISION_READ={'list_revision_runs':Change,'get_revision_run':Run,'compare_revision_run':Run}
REVISION_WRITE={'prepare_revision_run':Prepare,'start_revision_run':Run}
REVISION_DESCRIPTIONS={
 'list_revision_runs':'List owned candidate training runs for one applied code change; no SSH or training.',
 'get_revision_run':'Read owned candidate run plan, source digest and original-run linkage.',
 'compare_revision_run':'Read saved original/candidate reports and compare matching evaluation/dataset/split/step. No sync or generation.',
 'prepare_revision_run':'On current explicit human start instruction naming change_id, inspect candidate and save a run with original evaluation/parameters plus explicit overrides. Does not train.',
 'start_revision_run':'Start an owned prepared candidate run on current explicit human start instruction naming its change_id or run_id. Same run/retry never trains twice.'}
REVISION_POLICY='''修改后训练使用 list_revision_runs/get_revision_run 查询；需要比较结果使用 compare_revision_run，不猜 ID 或指标。
用户明确说“启动修改副本 <change_id> 的训练”时先 prepare_revision_run，再 start_revision_run；已有计划且明确给出 run_id 可直接启动。只允许本轮当前人类原文授权，一轮最多一个训练。准备/queued/running 不代表完成。
SOURCE_CHANGED 或 REVISION_SOURCE_CHANGED 需要核对修改副本后用新 request 创建新运行；旧运行不会自动重跑。网络超时只能查询/重试原 run，不能偷偷创建新运行。checks incomplete 必须说明，不能声称测试全部通过。
对比仅使用保存报告，相同声明不是数据文件内容相同的证明，单次指标改善不是代码修改产生因果改善的证明。'''


def projection(row):
    return {key:row[key] for key in ('run_id','original_run_id','change_id','status','parameters','server_id','script_id',
        'mode','candidate_directory','source_digest','source_file_count','check_status','notice')}
