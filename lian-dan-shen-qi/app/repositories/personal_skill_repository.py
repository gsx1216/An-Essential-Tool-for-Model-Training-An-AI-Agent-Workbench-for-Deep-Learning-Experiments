"""个人 Skill 保存在 MySQL；读取始终限定登录用户，正文不写入日志。"""
from pathlib import Path
from uuid import uuid4

from app.database import get_connection
from app.logging_config import logged_operation
from app.models.skill import PersonalSkillCreate, SkillMetadata, SkillSummary, SkillSnapshot, saved_snapshot, SkillNotFound, SkillStorageError
from app.validators import validate_positive_integer


class PersonalSkillRepository:
    def __init__(self,password,cache=None):
        self.password=password
        self.cache=cache
        root=Path(__file__).resolve().parents[2]/'sql'
        for name in ('insert_personal_skill','get_personal_skill','list_personal_skills','get_personal_skill_identity','delete_personal_skill'):
            setattr(self,name+'_sql',(root/(name+'.sql')).read_text(encoding='utf-8'))

    @logged_operation('personal_skill_repository.create')
    def create(self,user_id,request):
        user_id=validate_positive_integer(user_id,'用户 ID')
        request=PersonalSkillCreate.model_validate(request)
        metadata=SkillMetadata(name='personal-'+uuid4().hex,title=request.title,
            description='个人工作流：'+request.title,version='1.0.0')
        snapshot=SkillSnapshot.freeze(metadata,request.instructions)
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(self.insert_personal_skill_sql,(snapshot.skill_id,user_id,snapshot.model_dump_json()))
                connection.commit()
            except Exception:
                connection.rollback();raise
        return snapshot

    @logged_operation('personal_skill_repository.get',log_success=False)
    def get(self,user_id,skill_id):
        user_id=validate_positive_integer(user_id,'用户 ID')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    if self.cache is not None:
                        # 数据库确认归属与存在性；共享锁防止删除后并发读取重新填入永久缓存。
                        cursor.execute(self.get_personal_skill_identity_sql,(user_id,skill_id))
                        identity=cursor.fetchone()
                        if identity is None:raise SkillNotFound('Skill不存在或不可访问。')
                        cached=self.cache.load(user_id,skill_id,identity[0])
                        if cached is not None:
                            connection.commit()
                            return cached
                    cursor.execute(self.get_personal_skill_sql,(user_id,skill_id));row=cursor.fetchone()
                    if row is None:raise SkillNotFound('Skill不存在或不可访问。')
                    snapshot=saved_snapshot(row[0])
                    if snapshot.skill_id != skill_id:raise SkillStorageError('Skill标识与持久记录不一致。')
                    if self.cache is not None:self.cache.store(user_id,snapshot)
                connection.commit()
                return snapshot
            except Exception:
                connection.rollback();raise

    @logged_operation('personal_skill_repository.delete')
    def delete(self,user_id,skill_id):
        user_id=validate_positive_integer(user_id,'用户 ID')
        with get_connection(self.password) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(self.delete_personal_skill_sql,(user_id,skill_id))
                    deleted=cursor.rowcount==1
                connection.commit()
            except Exception:
                connection.rollback();raise
        if self.cache is not None:self.cache.delete(user_id,skill_id)
        if not deleted:raise SkillNotFound('Skill不存在或不可访问。')

    @logged_operation('personal_skill_repository.list',log_success=False)
    def list(self,user_id):
        user_id=validate_positive_integer(user_id,'用户 ID')
        with get_connection(self.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.list_personal_skills_sql,(user_id,));rows=cursor.fetchall()
        try:
            return [SkillSummary.model_validate_json(row[0]) for row in rows]
        except ValueError:
            raise SkillStorageError('已保存的Skill列表无法核对。') from None
