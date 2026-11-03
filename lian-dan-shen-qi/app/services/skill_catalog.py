"""合并部署目录与本人 MySQL Skill；个人标识必须携带当前用户。"""
from pathlib import Path
import re

import yaml

from app.logging_config import logged_operation
from app.models.skill import (SKILL_ID_PATTERN, MAX_SKILL_FILE_BYTES, SkillMetadata, SkillSnapshot,
                              SkillNotFound, SkillConfigurationError)


class SkillCatalog:
    def __init__(self, root, personal_repository=None):
        self.root = Path(root).resolve()
        self.personal_repository=personal_repository

    @logged_operation('skill_catalog.get', log_success=False)
    def get(self, skill_id, user_id=None):
        if not isinstance(skill_id, str) or not re.fullmatch(SKILL_ID_PATTERN, skill_id):
            raise SkillNotFound('Skill不存在或未启用。')
        if skill_id.startswith('personal-'):
            if self.personal_repository is None or user_id is None:
                raise SkillNotFound('Skill不存在或不可访问。')
            return self.personal_repository.get(user_id,skill_id)
        path = self.root / skill_id / 'SKILL.md'
        try:
            if not path.resolve().is_relative_to(self.root): raise SkillNotFound('Skill不存在或未启用。')
            with path.open('rb') as source: raw = source.read(MAX_SKILL_FILE_BYTES + 1)
        except FileNotFoundError:
            raise SkillNotFound('Skill不存在或未启用。') from None
        except OSError:
            raise SkillConfigurationError('Skill配置暂时不可用。') from None
        try:
            if len(raw) > MAX_SKILL_FILE_BYTES: raise ValueError('size')
            text = raw.decode('utf-8').replace('\r\n', '\n')
            if not text.startswith('---\n'): raise ValueError('header')
            header, separator, body = text[4:].partition('\n---\n')
            if not separator: raise ValueError('header delimiter')
            metadata = SkillMetadata.model_validate(yaml.safe_load(header))
            if metadata.name != skill_id: raise ValueError('directory mismatch')
            return SkillSnapshot.freeze(metadata, body.strip())
        except (ValueError, TypeError, UnicodeError, yaml.YAMLError):
            # 不把YAML解析器中可能含正文的原始异常传给HTTP或日志。
            raise SkillConfigurationError('Skill配置不符合要求。') from None

    @logged_operation('skill_catalog.list', log_success=False)
    def list(self, user_id=None):
        try:
            entries = sorted(self.root.iterdir()) if self.root.exists() else []
            identifiers = [p.name for p in entries if re.fullmatch(SKILL_ID_PATTERN, p.name) and (p/'SKILL.md').is_file()]
            if len(identifiers) > 50: raise SkillConfigurationError('Skill目录超过当前支持数量。')
        except OSError:
            raise SkillConfigurationError('Skill配置暂时不可用。') from None
        deployed=[self.get(identifier).summary() for identifier in identifiers if not identifier.startswith('personal-')]
        return deployed + (self.personal_repository.list(user_id) if self.personal_repository is not None and user_id is not None else [])

    def create(self,user_id,request):
        if self.personal_repository is None:
            raise SkillConfigurationError('个人Skill存储尚未配置。')
        return self.personal_repository.create(user_id,request)

    def delete(self,user_id,skill_id):
        # 公共部署模板不属于个人记录，不允许用户删除它们。
        if not isinstance(skill_id,str) or not re.fullmatch(SKILL_ID_PATTERN,skill_id) or not skill_id.startswith('personal-'):
            raise SkillNotFound('个人Skill不存在或不可访问。')
        if self.personal_repository is None:raise SkillConfigurationError('个人Skill存储尚未配置。')
        return self.personal_repository.delete(user_id,skill_id)
