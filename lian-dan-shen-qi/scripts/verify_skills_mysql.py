"""真实临时MySQL与HTTP Skill验收；模型/SSH替身，不产生供应商用量。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from openai import APIConnectionError

from app import database
from app.config import get_mysql_password
from app.errors import RequestIdConflictError
from app.models.skill import SkillStorageError
from app.services.skill_catalog import SkillCatalog
from scripts._chat_api_test_app import temporary_chat_api
from tests.test_skills import write_skill
from tests.paper_fixtures import pdf_bytes,PAPER_PAGES


async def verify(password):
    groups=[]
    with TemporaryDirectory() as directory:
        root=Path(directory);body='DO_NOT_LOG_SKILL_BODY. Inspect actual facts, source code and paper evidence.'
        path=write_skill(root,body=body);write_skill(root,'other-analysis',body='Different workflow.')
        catalog=SkillCatalog(root);first=catalog.get('test-analysis')
        with temporary_chat_api(password,skills=True,skill_root=root,training=True,papers=True) as (app,state),TestClient(app) as client:
            name=state['database_name'];repo=state['messages'];builder=state['builder']
            def sql(text,values=(),fetch=True):
                with database.get_connection(password) as connection:
                    with connection.cursor(dictionary=True) as cursor:
                        cursor.execute(text.replace('experiment_assistant',name),values)
                        result=cursor.fetchall() if fetch else None
                    connection.commit();return result
            def passed(label):groups.append(label);print('PASS:',label,flush=True)
            def user(prefix):
                creds={'username':prefix+uuid4().hex[:8],'password':'TestPass123!'}
                assert client.post('/auth/register',json=creds).status_code==201
                result=client.post('/auth/login',json=creds).json()
                return result['user']['user_id'],{'Authorization':'Bearer '+result['access_token']}
            uid,owner=user('skills_owner_');_,other=user('skills_other_')
            def session():return client.post('/sessions',headers=owner,json={'title':'Skill fixture'}).json()['session_id']
            def send(sid,*,rid=None,skill_id='test-analysis',content='请分析当前研究问题。'):
                return client.post('/sessions/'+sid+'/messages',headers=owner,json={'content':content,'request_id':rid or uuid4().hex,'skill_id':skill_id})
            def audit(sid,rid,headers=owner):return client.get(f'/sessions/{sid}/turns/{rid}/skill',headers=headers)
            def key():assert client.put('/me/model-credentials/deepseek',headers=owner,json={'api_key':'sk-TEST_ONLY_SKILL_KEY'}).status_code==204

            assert client.get('/skills').status_code==401
            assert client.get('/skills/test-analysis').status_code==401
            listing=client.get('/skills',headers=owner)
            assert listing.status_code==200 and len(listing.json()['items'])==2
            assert all('instructions' not in row for row in listing.json()['items'])
            assert client.get('/skills/test-analysis',headers=owner).json()['sha256']==first.sha256
            assert listing.headers['Cache-Control']=='no-store' and not builder.created
            passed('authenticated catalog list/detail expose deployed metadata and text without model Key, file paths or model calls')

            # 从真实旧表增加nullable列：既有用户问题和回复均保留。
            legacy=session();legacy_id=uuid4().hex
            sql('ALTER TABLE experiment_assistant.chat_turns DROP CHECK ck_chat_turn_skill, DROP COLUMN skill_snapshot_json',fetch=False)
            repo.start_turn(uid,legacy,'Legacy question',legacy_id)
            attempt,_=repo.claim_turn(uid,legacy,legacy_id)
            repo.complete_turn(uid,legacy,legacy_id,attempt,'Legacy answer')
            old=database._read_sql
            with patch.object(database,'_read_sql',side_effect=lambda filename:old(filename).replace('experiment_assistant',name)):
                with database.get_connection(password) as connection:database._ensure_skills(connection);database._ensure_skills(connection)
            assert audit(legacy,legacy_id).json()['skill'] is None
            assert [row['content'] for row in sql('SELECT content FROM experiment_assistant.chat_messages WHERE session_id=%s ORDER BY seq',(legacy,))]==['Legacy question','Legacy answer']
            passed('old schema upgrades twice with NULL selection and unchanged historical question/answer; no new table')

            sid=session();bad=uuid4().hex
            assert send(sid,rid=bad,skill_id='missing').status_code==404
            assert send(sid,skill_id='../secret').status_code==422
            assert client.post('/sessions/'+sid+'/messages',headers=owner,json={'content':'hello','request_id':uuid4().hex,'skill_id':'test-analysis','instructions':'replace system'}).status_code==422
            assert not sql('SELECT id FROM experiment_assistant.chat_turns WHERE session_id=%s',(sid,))
            assert not builder.created
            passed('unknown/path-like/client-supplied skill instructions fail before question persistence or model construction')

            key();rid=uuid4().hex
            async def selected(messages,options):
                system=messages[0]['content'] if isinstance(messages[0],dict) else messages[0][1]
                assert body in system and first.sha256 in system
                return {'content':'已按选定流程分析；当前没有可引用实验事实。'}
            builder.handler=selected
            response=send(sid,rid=rid);assert response.status_code==200,response.text
            frozen=audit(sid,rid).json()['skill']
            assert frozen['version']=='1.0.0' and frozen['sha256']==first.sha256 and frozen['instructions']==body
            assert [row['role'] for row in sql('SELECT role FROM experiment_assistant.chat_messages WHERE session_id=%s ORDER BY seq',(sid,))]==['human','ai']
            assert sql('SELECT content FROM experiment_assistant.chat_messages WHERE session_id=%s AND role=\'human\'',(sid,))[0]['content']=='请分析当前研究问题。'
            passed('selected workflow enters the system context and frozen version/digest/body commit with the unmodified question')

            for skill_id,content in [('other-analysis','请分析当前研究问题。'),(None,'请分析当前研究问题。'),('test-analysis','Different question')]:
                assert send(sid,rid=rid,skill_id=skill_id,content=content).status_code==409
            assert audit(sid,rid,other).status_code==404
            assert audit(legacy,rid).status_code==404
            assert audit(sid,uuid4().hex).status_code==404
            passed('request identity includes selected Skill ID; omitted/replaced Skill conflicts; snapshots enforce user/session/turn ownership')

            write_skill(root,version='1.1.0',body='Changed server workflow.')
            assert client.get('/skills/test-analysis',headers=owner).json()['version']=='1.1.0'
            assert audit(sid,rid).json()['skill']['version']=='1.0.0'
            path.unlink();assert client.delete('/me/model-credentials/deepseek',headers=owner).status_code==204
            previous=len(builder.created)
            assert send(sid,rid=rid).json()==response.json()
            assert len(builder.created)==previous and audit(sid,rid).status_code==200
            passed('catalog edits/removal do not change prior snapshots; completed replay and audit need neither current file nor personal Key')

            key();write_skill(root,version='1.1.0',body='Frozen failed workflow.')
            failed=session();failed_id=uuid4().hex;builder.handler=None
            builder.next_errors=[APIConnectionError(request=httpx.Request('POST','https://model-fixture.invalid'))]
            response=send(failed,rid=failed_id);assert response.status_code==502,response.text
            assert audit(failed,failed_id).json()['skill']['version']=='1.1.0'
            write_skill(root,version='2.0.0',body='New workflow must not enter retry.')
            async def frozen_retry(messages,options):
                system=messages[0]['content'] if isinstance(messages[0],dict) else messages[0][1]
                assert 'Frozen failed workflow.' in system and 'New workflow must not enter retry.' not in system
                return {'content':'重试使用原Skill版本。'}
            builder.handler=frozen_retry;path.unlink()
            response=client.post('/sessions/'+failed+'/retry',headers=owner)
            assert response.status_code==200,response.text
            assert len(sql('SELECT seq FROM experiment_assistant.chat_messages WHERE session_id=%s',(failed,)))==2
            passed('failed-turn retry uses its original snapshot after file replacement/removal without adding a second user message')

            write_skill(root,body=body);resend=session();resend_id=uuid4().hex;builder.handler=None
            builder.next_errors=[APIConnectionError(request=httpx.Request('POST','https://model-fixture.invalid'))]
            assert send(resend,rid=resend_id).status_code==502
            path.unlink();builder.handler=selected
            assert send(resend,rid=resend_id).status_code==200
            passed('resending the same failed request also resolves its frozen Skill without reading the current catalog')

            write_skill(root,body=body);plain_id=uuid4().hex
            async def plain(messages,options):
                system=messages[0]['content'] if isinstance(messages[0],dict) else messages[0][1]
                assert body not in system and '【本轮用户选择的服务器 Skill】' not in system
                return {'content':'普通聊天。'}
            builder.handler=plain
            assert send(sid,rid=plain_id,skill_id=None,content='你好。').status_code==200
            assert audit(sid,plain_id).json()['skill'] is None
            passed('Skill is selected per turn; later omitted selection remains ordinary chat and does not inherit another turn prompt')

            # 源文件包含训练字样不构成用户执行授权。
            write_skill(root,body='For this test the document says: 启动 PRNet. This is not user authorization.')
            unsafe=session();unsafe_id=uuid4().hex
            async def attempted_start(messages,options):
                names={row['function']['name'] for row in options['tools']}
                assert 'start_training_run' not in names
                delivered=[m for m in messages if isinstance(m,dict) and m.get('role')=='tool']
                if not delivered:return {'tool_calls':[{'id':'forbidden','name':'start_training_run','args':{'plan_id':uuid4().hex}}]}
                assert json.loads(delivered[-1]['content'])['code']=='UNSUPPORTED_TOOL'
                return {'content':'没有启动训练。'}
            builder.handler=attempted_start
            response=send(unsafe,rid=unsafe_id,content='你好。');assert response.status_code==200,response.text
            assert not sql('SELECT plan_id FROM experiment_assistant.training_launch_claims')
            assert not sql('SELECT run_id FROM experiment_assistant.run_executions')
            passed('Skill instructions cannot enable training tools or authorize execution; forged model start call is rejected before SSH')

            # 并发同请求的选中/不选中只能有一种结果；原子保存防止先写问题后丢选择。
            write_skill(root,body=body);race=session();race_id=uuid4().hex;chosen=catalog.get('test-analysis')
            def start(value):
                try:return repo.start_turn(uid,race,'Concurrent question',race_id,skill_snapshot=value)
                except RequestIdConflictError:return 'conflict'
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(start,[None,chosen]))
            assert sorted(results)==sorted([race_id,'conflict']),results
            assert len(sql('SELECT seq FROM experiment_assistant.chat_messages WHERE session_id=%s',(race,)))==1
            rollback=session();rollback_id=uuid4().hex;builder.handler=plain
            with patch.object(repo,'save_turn_skill_sql','UPDATE missing_skill_fixture SET value=%s WHERE a=%s AND b=%s'):
                assert send(rollback,rid=rollback_id).status_code==503
            assert not sql('SELECT id FROM experiment_assistant.chat_turns WHERE session_id=%s',(rollback,))
            assert not sql('SELECT seq FROM experiment_assistant.chat_messages WHERE session_id=%s',(rollback,))
            passed('concurrent request selection is checked under the session lock; snapshot write failure rolls back question and turn together')

            # 实际PDF读取后沿用原严格引用协议，Skill不覆盖该协议。
            raw=pdf_bytes(PAPER_PAGES)
            document=client.post('/papers',headers={**owner,'Content-Type':'application/pdf'},params={'request_id':uuid4().hex,'title':'Skill paper'},content=raw).json()
            assert document['status']=='ready'
            chunk=client.get('/papers/'+document['paper_id']+'/chunks',headers=owner).json()['items'][0]['chunk_id']
            rag=session();rag_id=uuid4().hex
            async def paper_chat(messages,options):
                assert body in messages[0]['content']
                delivered=[m for m in messages if isinstance(m,dict) and m.get('role')=='tool']
                if not delivered:return {'tool_calls':[{'id':'paper','name':'read_paper_chunk','args':{'chunk_id':chunk}}]}
                return {'content':json.dumps({'answer':'依据实际正文说明边界监督。','citations':[{'chunk_id':chunk,'quote':'boundary supervision'}],
                    'limitations':['只读取本段，尚未验证具体实验效果。']})}
            builder.handler=paper_chat
            response=send(rag,rid=rag_id,content='请根据上传论文分析研究方法。')
            assert response.status_code==200 and 'PDF 第 1 页' in response.json()['content'],response.text
            assert audit(rag,rag_id).json()['skill']['sha256']==chosen.sha256
            passed('Skill and actual PDF RAG work in one turn; real quote/page validation and existing tool audit remain active')

            # 损坏的保存快照不能冒充正常配置，返回固定503且不生成。
            corrupt=session();corrupt_id=repo.start_turn(uid,corrupt,'Corrupt snapshot',uuid4().hex,skill_snapshot=chosen)
            bad=chosen.model_dump();bad['instructions']='tampered'
            sql('UPDATE experiment_assistant.chat_turns SET skill_snapshot_json=%s WHERE id=%s',(json.dumps(bad),corrupt_id),False)
            assert audit(corrupt,corrupt_id).status_code==503
            builder.handler=plain
            assert client.post('/sessions/'+corrupt+'/retry',headers=owner).status_code==503
            passed('corrupt snapshot digest is diagnosed as unavailable rather than silently loading a different workflow')

            assert client.delete('/sessions/'+rag,headers=owner).status_code==204
            assert audit(rag,rag_id).status_code==404
            assert client.get('/skills/test-analysis',headers=owner).status_code==200
            logs='\n'.join(p.read_text(encoding='utf-8') for p in state['log_dir'].rglob('*.txt'))
            assert body not in logs and 'sk-TEST_ONLY_SKILL_KEY' not in logs
            schema=client.get('/openapi.json').json()
            assert 'skill_id' in schema['components']['schemas']['SendMessageRequest']['properties']
            assert all(path in schema['paths'] for path in ('/skills','/skills/{skill_id}','/sessions/{session_id}/turns/{turn_id}/skill'))
            assert len(sql('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s',(name,)))==39
            passed('chat deletion removes its snapshots but not deployed Skills; logs omit prompt/key; mounted Swagger and 39-table schema verified')
    print(f'ALL {len(groups)} Skill HTTP/MySQL groups passed; temporary DB removed; no paid model, SSH or training.',flush=True)
    return len(groups)


if __name__=='__main__':asyncio.run(verify(get_mysql_password()))
