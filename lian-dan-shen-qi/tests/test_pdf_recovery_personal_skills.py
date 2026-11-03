"""覆盖本次实际故障和个人 Skill 权限，不调用模型或训练。"""
import asyncio
from datetime import datetime,timezone
import os
import io
import logging
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_skill_catalog
from app.api.schemas import UserResponse
from app.api.skill_routes import router
from app.models.paper import PaperStorageError
from app.models.paper import PaperParseError
from app.logging_config import SafeTextFormatter,log_failure,logging_context
from app.models.skill import PersonalSkillCreate,SkillNotFound,SkillMetadata,SkillSnapshot
from app.repositories.paper_repository import PaperRepository
from app.repositories.personal_skill_repository import PersonalSkillRepository
from app.services.paper_service import PaperService
from app.services.pdf_parser import PdfParser
from app.services.skill_catalog import SkillCatalog
from tests.paper_fixtures import pdf_bytes,PAPER_PAGES


class PdfRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows reload regression')
    def test_actual_pdf_from_windows_selector_loop(self):
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            result=runner.run(PdfParser().parse(pdf_bytes(PAPER_PAGES)))
        self.assertEqual(result['page_count'],2)

    def test_stale_processing_is_visible_as_retryable(self):
        now=datetime.now(timezone.utc)
        row=dict(id='a'*32,request_id='b'*32,title='Paper',status='processing',error_code=None,
            file_sha256='c'*64,file_bytes=12,page_count=0,chunk_count=0,empty_pages_json='[]',
            parser_version=None,created_at=now,updated_at=now,lease_active=0)
        decoded=PaperRepository.decode(row)
        self.assertEqual(decoded['status'],'failed')
        self.assertEqual(decoded['error_code'],'PROCESS_INTERRUPTED')
        row['lease_active']=1
        self.assertEqual(PaperRepository.decode(row)['status'],'processing')


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_error_does_not_leave_processing(self):
        repo=MagicMock()
        service=PaperService(repo,parser=MagicMock(parse=AsyncMock(side_effect=NotImplementedError())))
        with self.assertRaises(PaperStorageError):
            await service._process(1,{'paper_id':'a'*32},'token',b'%PDF-test')
        repo.fail.assert_called_once_with(1,'a'*32,'token','PROCESS_INTERRUPTED')


class PersonalSkillTests(unittest.TestCase):
    def test_blank_or_oversize_instructions_rejected(self):
        for value in (' ','中'*2049):
            with self.assertRaises(ValueError):PersonalSkillCreate(title='Test',instructions=value)

    def test_repository_reads_scoped_to_user_and_rejects_missing(self):
        repo=PersonalSkillRepository('not-a-real-password')
        connection=MagicMock();connection.__enter__.return_value=connection
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value=None
        with patch('app.repositories.personal_skill_repository.get_connection',return_value=connection):
            with self.assertRaises(SkillNotFound):repo.get(2,'personal-'+ 'a'*32)
        cursor=connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        # connection.__enter__ is the actual cursor owner in the repository.
        self.assertEqual(cursor.execute.call_args.args[1],(2,'personal-'+'a'*32))

    def test_catalog_personal_id_never_falls_back_to_other_users(self):
        with TemporaryDirectory() as root:
            repository=MagicMock();repository.get.side_effect=SkillNotFound('missing')
            catalog=SkillCatalog(root,repository)
            with self.assertRaises(SkillNotFound):catalog.get('personal-'+'a'*32,2)
            repository.get.assert_called_once_with(2,'personal-'+'a'*32)
            with self.assertRaises(SkillNotFound):catalog.get('personal-'+'a'*32)

    def test_http_create_requires_login_and_passes_authenticated_identity(self):
        snapshot=SkillSnapshot.freeze(SkillMetadata(name='personal-'+'a'*32,title='Test',description='Test',version='1.0.0'),'Read evidence first.')
        catalog=MagicMock();catalog.create.return_value=snapshot;catalog.get.return_value=snapshot
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_skill_catalog]=lambda:catalog
        with TestClient(app) as client:
            self.assertEqual(client.post('/skills',json={'title':'Test','instructions':'Read evidence first.'}).status_code,401)
            app.dependency_overrides[require_current_user]=lambda:UserResponse(user_id=2,username='test')
            response=client.post('/skills',json={'title':'Test','instructions':'Read evidence first.'})
            self.assertEqual(response.status_code,201)
            self.assertEqual(catalog.create.call_args.args[0],2)
            self.assertEqual(client.get('/skills/'+snapshot.skill_id).status_code,200)
            catalog.get.assert_called_once_with(snapshot.skill_id,2)


class PaperLoggingTests(unittest.TestCase):
    def test_log_names_pdf_cause_and_location_without_including_body(self):
        output=io.StringIO();handler=logging.StreamHandler(output);handler.setFormatter(SafeTextFormatter())
        logger=logging.getLogger('paper-regression-test');logger.setLevel(logging.DEBUG);logger.addHandler(handler)
        self.addCleanup(logger.removeHandler,handler)
        with logging_context(new=True):
            try:raise PaperParseError('NO_EXTRACTABLE_TEXT')
            except PaperParseError as error:log_failure(logger,error,operation='paper_service.parse')
        text=output.getvalue()
        self.assertIn('paper_code=NO_EXTRACTABLE_TEXT',text)
        self.assertIn('扫描件需先进行 OCR',text)
        self.assertIn('test_pdf_recovery_personal_skills.py',text)
        self.assertIn('request_id=',text)
