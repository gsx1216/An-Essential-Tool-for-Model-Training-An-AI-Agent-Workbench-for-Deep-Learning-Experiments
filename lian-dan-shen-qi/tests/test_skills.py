"""Skill目录、不可变快照、重试与输入预算；不调用真实模型。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, create_autospec

from app.api.schemas import SendMessageRequest
from app.errors import RequestIdConflictError, SessionNotFoundError
from app.models.context_settings import ContextSettings
from app.models.context_snapshot import ContextAttempt
from app.models.context_state import ContextMessage
from app.models.skill import (SkillMetadata,SkillSnapshot,SkillRequestState,SkillConfigurationError,
                              SkillNotFound,SkillStorageError,saved_snapshot)
from app.repositories.message_repository import MessageRepository
from app.services.chat_context_factory import ChatContextFactory
from app.services.skill_catalog import SkillCatalog
from app.services.user_chat_service import UserChatService

SID,TID,AID='a'*32,'b'*32,'c'*32


def write_skill(root,name='test-analysis',*,version='1.0.0',body='Check facts and actual source before proposing changes.'):
    path=Path(root)/name/'SKILL.md';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(f'---\nname: {name}\ntitle: Test analysis\nversion: "{version}"\ndescription: A research workflow.\n---\n{body}\n',encoding='utf-8')
    return path


def snapshot():
    return SkillSnapshot.freeze(SkillMetadata(name='test-analysis',title='Test',version='1.0.0',description='Workflow.'),'Check actual facts first.')


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.catalog=SkillCatalog(self.root)
        self.path=write_skill(self.root)

    def test_catalog_and_snapshot_keep_id_version_body_and_digest(self):
        item=self.catalog.get('test-analysis')
        self.assertEqual(self.catalog.list()[0],item.summary())
        self.assertEqual(saved_snapshot(item.model_dump_json()),item)
        self.assertNotIn('instructions',item.summary().model_dump())

    def test_deployer_edits_are_visible_without_mutating_existing_snapshot(self):
        original=self.catalog.get('test-analysis')
        write_skill(self.root,version='1.1.0',body='Changed workflow.')
        changed=self.catalog.get('test-analysis')
        self.assertNotEqual(original.sha256,changed.sha256)
        self.assertEqual(original.version,'1.0.0')
        self.assertEqual(changed.version,'1.1.0')
        with self.assertRaises(ValueError):original.instructions='replacement'

    def test_missing_or_path_like_identifiers_do_not_open_arbitrary_files(self):
        for value in ('../test-analysis','/etc/passwd','C:\\secret','test-analysis/SKILL.md','missing',None):
            with self.subTest(value=value),self.assertRaises(SkillNotFound):self.catalog.get(value)

    def test_yaml_executable_tags_unknown_fields_and_mismatched_name_rejected(self):
        original=self.path.read_text(encoding='utf-8')
        for text in (original.replace('name: test-analysis','name: another'),
                     original.replace('title: Test analysis','title: !!python/object/apply:os.system [echo forbidden]'),
                     original.replace('title: Test analysis','title: Test analysis\ncommand: anything'),
                     original.replace('version: "1.0.0"','version: 1.0')):
            self.path.write_text(text,encoding='utf-8')
            with self.subTest(text=text[:30]),self.assertRaises(SkillConfigurationError):self.catalog.get('test-analysis')

    def test_file_and_instruction_limits_fail_before_model_use(self):
        for body in ('x'*9000,'中'*2100,'  '):
            write_skill(self.root,body=body)
            with self.subTest(size=len(body)),self.assertRaises(SkillConfigurationError):self.catalog.get('test-analysis')

    def test_bad_header_and_broken_utf8_are_configuration_failures(self):
        for raw in (b'no header',b'---\nname: x',b'\xff'):
            self.path.write_bytes(raw)
            with self.assertRaises(SkillConfigurationError):self.catalog.get('test-analysis')

    def test_saved_content_or_version_tamper_is_not_silently_loaded(self):
        value=snapshot().model_dump()
        for field in ('instructions','version','sha256'):
            tampered=deepcopy(value);tampered[field]='changed'
            with self.subTest(field=field),self.assertRaises(SkillStorageError):saved_snapshot(json.dumps(tampered))

    def test_empty_catalog_and_omitted_selection_remain_supported(self):
        self.path.unlink();self.assertEqual(self.catalog.list(),[])
        request=SendMessageRequest(content='Hello',request_id=TID)
        self.assertIsNone(request.skill_id)
        with self.assertRaises(ValueError):SendMessageRequest(content='Hello',request_id=TID,skill_id='../secret')
        with self.assertRaises(ValueError):SendMessageRequest(content='Hello',request_id=TID,skill_id='test-analysis',instructions='replace system')


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_is_counted_before_context_preparation(self):
        factory=ChatContextFactory(MagicMock(),MagicMock(),ContextSettings())
        normal,_=factory.create(1,SID,ContextAttempt(TID,AID))
        chosen=snapshot();skilled,_=factory.create(1,SID,ContextAttempt(TID,AID),skill_snapshot=chosen)
        self.assertNotIn(chosen.instructions,normal.manager.system_prompt)
        self.assertIn(chosen.instructions,skilled.manager.system_prompt)
        plain=await normal.manager.prepare([ContextMessage(1,'human','Hello')],None)
        prepared=await skilled.manager.prepare([ContextMessage(1,'human','Hello')],None)
        counter=skilled.manager.counter
        self.assertGreater(counter.count_messages(prepared.model_messages),counter.count_messages(plain.model_messages))
        self.assertEqual(prepared.model_messages[0][0],'system')


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=create_autospec(MessageRepository,instance=True,spec_set=True)
        self.repo.inspect_skill_request.return_value=SkillRequestState(False)
        self.catalog=SimpleNamespace(get=MagicMock(return_value=snapshot()))
        self.gateway=SimpleNamespace(invoke=AsyncMock(return_value={'content':'answer'}),aclose=AsyncMock())
        self.gateway_factory=SimpleNamespace(create_for_user=AsyncMock(return_value=self.gateway))
        self.service=UserChatService(self.gateway_factory,self.repo,skill_catalog=self.catalog)
        self.repo.start_turn.return_value=TID;self.repo.claim_turn.return_value=(AID,[('human','question')])
        self.repo.load_turn_skill.return_value=snapshot()

    async def test_new_selection_is_frozen_with_question_before_model(self):
        result=await self.service.send_message(1,SID,'question',TID,skill_id='test-analysis')
        self.assertEqual(result,'answer')
        self.assertEqual(self.repo.start_turn.call_args.kwargs['skill_snapshot'],snapshot())
        self.assertIn(snapshot().instructions,self.gateway.invoke.call_args.args[0][0][1])

    async def test_completed_replay_needs_neither_catalog_nor_key(self):
        self.repo.inspect_skill_request.return_value=SkillRequestState(True,'saved',snapshot())
        self.assertEqual(await self.service.send_message(1,SID,'question',TID,skill_id='test-analysis'),'saved')
        self.catalog.get.assert_not_called();self.gateway_factory.create_for_user.assert_not_awaited()

    async def test_existing_failed_request_uses_frozen_snapshot_without_catalog(self):
        self.repo.inspect_skill_request.return_value=SkillRequestState(True,None,snapshot())
        await self.service.send_message(1,SID,'question',TID,skill_id='test-analysis')
        self.catalog.get.assert_not_called()
        self.assertEqual(self.repo.start_turn.call_args.kwargs['skill_snapshot'].version,'1.0.0')

    async def test_bad_selection_and_not_owned_request_stop_before_key(self):
        for error in (SessionNotFoundError('not owned'),RequestIdConflictError('different skill')):
            self.repo.inspect_skill_request.side_effect=error
            with self.assertRaises(type(error)):await self.service.send_message(1,SID,'question',TID,skill_id='test-analysis')
        self.gateway_factory.create_for_user.assert_not_awaited();self.repo.start_turn.assert_not_called()

    async def test_missing_catalog_entry_does_not_save_question(self):
        self.catalog.get.side_effect=SkillNotFound('missing')
        with self.assertRaises(SkillNotFound):await self.service.send_message(1,SID,'question',TID,skill_id='test-analysis')
        self.repo.start_turn.assert_not_called();self.gateway_factory.create_for_user.assert_not_awaited()

    async def test_none_explicitly_records_no_skill_and_does_not_load_catalog(self):
        self.repo.load_turn_skill.return_value=None
        await self.service.send_message(1,SID,'question',TID)
        self.assertIsNone(self.repo.start_turn.call_args.kwargs['skill_snapshot'])
        self.catalog.get.assert_not_called()
        self.assertEqual(self.gateway.invoke.call_args.args[0][0],('system','你是一个乐于助人的ai助手'))


if __name__=='__main__':unittest.main()
