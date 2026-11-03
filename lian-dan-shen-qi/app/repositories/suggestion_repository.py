"""复用分析任务生命周期；建议阶段独立开关、结果和工具证据，仍使用 agent_tasks。"""
from pathlib import Path

from app.database import get_connection
from app.logging_config import logged_operation
from app.models.analysis import AnalysisLeaseLostError, AnalysisStorageError, AnalysisConflictError, compact, strict_json
from app.models.suggestion import verify_suggestions, analysis_input, SuggestionContent
from app.models.research import content_hash
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.experiment_repository import _utc


class SuggestionRepository(AnalysisRepository):
    agent_name = 'suggestion'
    validate_content = staticmethod(verify_suggestions)

    def __init__(self, password):
        super().__init__(password)
        root = Path(__file__).resolve().parents[2] / 'sql'
        for name in ('get_analysis_preference', 'save_analysis_preference', 'reap_analysis_tasks',
                     'next_analysis_task', 'next_run_analysis_task', 'get_analysis_task', 'renew_analysis_task', 'load_analysis_source',
                     'save_analysis_result', 'list_analysis_results'):
            replacement = name.replace('analysis', 'suggestion')
            setattr(self, name + '_sql', (root / (replacement + '.sql')).read_text(encoding='utf-8'))
        for name in ('save_suggestion_evidence', 'list_suggestion_evidence', 'get_suggestion_result', 'clear_insufficient_suggestion'):
            setattr(self, name + '_sql', (root / (name + '.sql')).read_text(encoding='utf-8'))

    def source_row(self, row):
        source = super().source_row(row)
        analysis_input(source)
        return source

    def prepare_retry(self, cursor, row):
        if row['lease_active']:
            raise AnalysisConflictError('建议仍在处理中。')
        if row['status'] == 'succeeded':
            cursor.execute(self.get_suggestion_result_sql, (row['task_id'],))
            saved = cursor.fetchone()
            if not saved or content_hash(saved['content_json']) != saved['content_digest']:
                raise AnalysisStorageError('建议结果无法核对。')
            content = SuggestionContent.model_validate(strict_json(saved['content_json']))
            if content.evidence_status != 'insufficient' or content.suggestions or content.code_findings or content.direct_advice:
                raise AnalysisConflictError('已有具体建议，不覆盖成功结果。')
            # 用户显式重试仅替换零建议诊断；旧尝试工具证据仍保留。
            cursor.execute(self.clear_insufficient_suggestion_sql, (row['task_id'],))

    @logged_operation('suggestion_repository.record', log_success=False)
    def record(self, user_id, run_id, task_id, token, index, name, arguments, result):
        user_id, task_id, token = self._identity(user_id, task_id, token)
        if type(index) is not int or not 1 <= index <= 8 or name not in {
            'list_project_files', 'read_project_file', 'search_references', 'read_reference','list_papers','search_papers','read_paper_chunk', 'unsupported'}:
            raise ValueError('建议工具记录不符合要求。')
        args, payload = compact(arguments), compact(result)
        if len(args.encode()) > 2048 or len(payload.encode()) > 16384:
            raise AnalysisStorageError('建议工具记录超出预算。')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor(dictionary=True) as cursor:
                    self._owner(cursor, user_id, run_id, lock=True)
                    cursor.execute(self.get_analysis_task_sql.rstrip().rstrip(';') + ' FOR UPDATE', (user_id, run_id, task_id))
                    task = cursor.fetchone()
                    if not task or task['status'] != 'running' or task['lease_token'] != token or not task['lease_active']:
                        raise AnalysisLeaseLostError('建议生成尝试已失效。')
                    cursor.execute(self.get_analysis_preference_sql.rstrip().rstrip(';') + ' FOR SHARE', (user_id,))
                    pref = cursor.fetchone()
                    if (not pref or not pref['enabled']) and self._manual_scope is None:
                        raise AnalysisLeaseLostError('本人建议开关已关闭。')
                    cursor.execute(self.save_suggestion_evidence_sql, (task_id, token, index, name, args, payload))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @logged_operation('suggestion_repository.evidence')
    def evidence(self, user_id, run_id, task_id):
        # task 同时检查用户、运行和 agent_name，不能通过本接口读取另一阶段的任务。
        self.task(user_id, run_id, task_id)
        with get_connection(self.password) as connection:
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(self.list_suggestion_evidence_sql, (task_id.replace('-', ''),))
                rows = list(reversed(cursor.fetchall()))
                attempts = {}
                for row in rows:
                    token = row.pop('attempt_token')
                    row['attempt_number'] = attempts.setdefault(token, len(attempts) + 1)
                    row['arguments'] = strict_json(row.pop('arguments_json'))
                    row['result'] = strict_json(row.pop('result_json'))
                    row['created_at'] = _utc(row['created_at'])
                return rows
