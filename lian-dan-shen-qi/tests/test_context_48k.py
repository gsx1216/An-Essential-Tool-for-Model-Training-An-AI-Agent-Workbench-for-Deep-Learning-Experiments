"""48K 部署预算回归：真实计数和上下文管线，模拟报告与模型，不联网或启动训练。"""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config
from app.errors import ChatToolBudgetError
from app.models.chat_tools import request_size
from app.models.context_settings import ContextSettings
from app.models.context_snapshot import ContextAttempt
from app.models.context_state import ContextMessage, ContextTooLargeError
from app.services.chat_context_factory import ChatContextFactory
from app.services.context_manager import ContextManager
from app.services.read_only_chat_service import ReadOnlyChatService
from app.services.token_budget import Utf8BudgetCounter


SESSION = 'a' * 32
RUN = 'b' * 32
ATTEMPT = ContextAttempt('c' * 32, 'd' * 32)


class Context48KTests(unittest.IsolatedAsyncioTestCase):
    def factory(self, settings=None):
        tool_service = ReadOnlyChatService(
            MagicMock(), MagicMock(), metrics_factory=MagicMock(), runner_factory=MagicMock(),
            training_factory=MagicMock(),
        )
        return ChatContextFactory(
            SimpleNamespace(invoke=AsyncMock()), MagicMock(), settings or ContextSettings(),
            input_reserve=tool_service.input_reserve,
        )

    def history(self, size=1900):
        messages = []
        for index in range(4):
            messages.extend([
                ContextMessage(index * 2 + 1, 'human', '查看这轮训练的结果'),
                ContextMessage(index * 2 + 2, 'ai', '训练记录说明' * size),
            ])
        messages.append(ContextMessage(9, 'human', '查看报告'))
        return messages

    def test_defaults_and_direct_manager_share_48k_budget(self):
        settings = ContextSettings()
        manager = ContextManager(Utf8BudgetCounter(), MagicMock())
        self.assertEqual(settings.max_input_tokens, 49152)
        self.assertEqual(settings.model_request_tokens, 49152)
        self.assertEqual(settings.compact_at_tokens, 39321)
        self.assertEqual(manager.max_input_tokens, settings.max_input_tokens)
        self.assertEqual(manager.compact_at_tokens, settings.compact_at_tokens)
        context, _ = self.factory().create(7, SESSION, ATTEMPT)
        self.assertEqual(context.manager.max_input_tokens, 40152)
        self.assertEqual(context.manager.keep_recent_turns, 4)

    def test_environment_can_still_override_both_request_limits(self):
        with patch.object(config, 'load_dotenv'), patch.dict('os.environ', {
            'CHAT_MAX_INPUT_TOKENS': '60000', 'CHAT_MODEL_REQUEST_TOKENS': '49152',
            'CHAT_COMPACT_AT_TOKENS': '38000',
        }, clear=True):
            settings = config.get_context_settings()
        context, _ = self.factory(settings).create(7, SESSION, ATTEMPT)
        self.assertEqual(context.manager.max_input_tokens, 49152 - 9000)
        self.assertEqual(context.manager.compact_at_tokens, 38000)

    async def test_recent_history_above_old_limit_reaches_report_tool_and_answer(self):
        context, _ = self.factory().create(7, SESSION, ATTEMPT)
        # 四轮完整近期对话；总量约 24K UTF-8 字节，高于旧上限，低于新触发点。
        history = self.history(size=330)
        original = list(history)
        prepared = await context.manager.prepare(history)
        self.assertGreater(prepared.input_tokens, 12000)
        self.assertLess(prepared.input_tokens, 39321)
        self.assertFalse(prepared.was_compacted)
        self.assertEqual(history, original)
        self.assertEqual(prepared.model_messages[-1], ('human', '查看报告'))

        repo = MagicMock()
        repo.binding.return_value = RUN
        service = ReadOnlyChatService(repo, MagicMock(), metrics_factory=MagicMock(), runner_factory=MagicMock())
        evidence = {'ok': True, 'code': 'OK', 'run_id': RUN,
                    'data': {'report_version': 1, 'status': 'succeeded', 'mae': 0.03}}
        gateway = SimpleNamespace(invoke=AsyncMock(side_effect=[
            {'content': '', 'tool_calls': [{'id': 'report_1', 'name': 'get_run_report', 'args': {}}]},
            {'content': '报告显示训练成功，测试MAE为0.03。'},
        ]))
        with patch.object(service, '_execute', new=AsyncMock(return_value=(evidence, 'set', RUN))) as execute:
            answer = await service.answer(
                prepared.model_messages, user_id=7, session_id=SESSION, attempt=ATTEMPT,
                gateway=gateway, max_input_tokens=49152,
            )
        self.assertIn('0.03', answer)
        execute.assert_awaited_once()
        self.assertEqual(gateway.invoke.await_count, 2)
        self.assertEqual(repo.record.call_count, 1)
        final = gateway.invoke.call_args
        self.assertLessEqual(request_size(final.args[0], final.kwargs['tools']), 49152)

    async def test_old_small_override_reproduces_recent_history_error(self):
        settings = ContextSettings(max_input_tokens=12000, model_request_tokens=12000, compact_at_tokens=9600)
        context, _ = self.factory(settings).create(7, SESSION, ATTEMPT)
        with self.assertRaises(ContextTooLargeError):
            await context.manager.prepare(self.history(size=330))

    async def test_new_threshold_still_summarizes_old_turns_and_keeps_recent_four(self):
        context, _ = self.factory().create(7, SESSION, ATTEMPT)
        summarizer = SimpleNamespace(summarize=AsyncMock(return_value='较早两轮已确认训练目标与参数。'))
        context.manager.summarizer = summarizer
        history = []
        for index in range(6):
            history.extend([ContextMessage(index * 2 + 1, 'human', '请分析训练'),
                            ContextMessage(index * 2 + 2, 'ai', '训' * 2900)])
        history.append(ContextMessage(13, 'human', '查看报告'))
        original = list(history)
        prepared = await context.manager.prepare(history)
        self.assertTrue(prepared.was_compacted)
        self.assertLessEqual(prepared.input_tokens, context.manager.max_input_tokens)
        self.assertEqual(prepared.summary.summarized_until_seq, 4)
        self.assertEqual(prepared.model_messages[-9:], [(m.role, m.content) for m in history[4:]])
        self.assertEqual(history, original)
        summarizer.summarize.assert_awaited_once()
        self.assertEqual(summarizer.summarize.call_args.kwargs['messages'], history[:4])

    async def test_history_above_new_limit_still_refuses_without_dropping_recent_turns(self):
        context, _ = self.factory().create(7, SESSION, ATTEMPT)
        history = self.history(size=1900)
        original = list(history)
        with self.assertRaises(ContextTooLargeError):
            await context.manager.prepare(history)
        self.assertEqual(history, original)

    async def test_tool_request_still_has_a_hard_limit(self):
        repo = MagicMock()
        repo.binding.return_value = RUN
        gateway = SimpleNamespace(invoke=AsyncMock())
        service = ReadOnlyChatService(repo, MagicMock(), metrics_factory=MagicMock(), runner_factory=MagicMock())
        with self.assertRaises(ChatToolBudgetError):
            await service.answer(
                [('system', '助手'), ('human', 'x' * 49152)], user_id=7,
                session_id=SESSION, attempt=ATTEMPT, gateway=gateway, max_input_tokens=49152,
            )
        gateway.invoke.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
