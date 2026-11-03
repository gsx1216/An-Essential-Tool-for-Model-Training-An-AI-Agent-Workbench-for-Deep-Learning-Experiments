"""聊天接口数据模型回归；不连接数据库或模型。"""

import unittest
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.api.schemas import ChatReplyResponse, SendMessageRequest


class ChatApiModelChecks(unittest.TestCase):
    def test_request_declares_only_two_required_fields(self):
        self.assertEqual(set(SendMessageRequest.model_fields), {'content', 'request_id', 'skill_id'})
        self.assertIs(SendMessageRequest.model_fields['content'].annotation, str)
        self.assertIs(SendMessageRequest.model_fields['request_id'].annotation, UUID)
        self.assertEqual({name for name,field in SendMessageRequest.model_fields.items() if field.is_required()}, {'content','request_id'})
        self.assertIsNone(SendMessageRequest.model_fields['skill_id'].default)

    def test_valid_request_preserves_content_and_parses_uuid(self):
        request_id = uuid4()
        request = SendMessageRequest(content='  假问题\n', request_id=str(request_id))
        self.assertEqual(request.content, '  假问题\n')
        self.assertEqual(request.request_id, request_id)
        self.assertEqual(request.request_id.hex, request_id.hex)

    def test_each_required_field_must_be_supplied(self):
        for body in ({}, {'content': '假问题'}, {'request_id': uuid4().hex}):
            with self.subTest(fields=tuple(body)):
                with self.assertRaises(ValidationError):
                    SendMessageRequest(**body)

    def test_invalid_request_id_is_rejected(self):
        for request_id in ('bad', '', None, 123, True, []):
            with self.subTest(kind=type(request_id).__name__):
                with self.assertRaises(ValidationError):
                    SendMessageRequest(content='假问题', request_id=request_id)

    def test_invalid_content_type_is_rejected(self):
        for content in (None, 123, {'text': '假问题'}, ['假问题']):
            with self.subTest(kind=type(content).__name__):
                with self.assertRaises(ValidationError):
                    SendMessageRequest(content=content, request_id=uuid4())

    def test_owner_credentials_and_browser_history_are_not_accepted(self):
        for field in ('user_id', 'api_key', 'messages', 'history', 'session_id'):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    SendMessageRequest(content='假问题', request_id=uuid4(), **{field: 'TEST_ONLY'})

    def test_reply_has_one_required_string_field(self):
        self.assertEqual(set(ChatReplyResponse.model_fields), {'content'})
        field = ChatReplyResponse.model_fields['content']
        self.assertIs(field.annotation, str)
        self.assertTrue(field.is_required())
        with self.assertRaises(ValidationError):
            ChatReplyResponse()

    def test_reply_returns_only_public_content(self):
        self.assertEqual(ChatReplyResponse(content='假回复').model_dump(), {'content': '假回复'})

