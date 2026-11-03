-- 为旧 experiments 表补上运行归属复合外键需要的父键，不改写业务字段。
ALTER TABLE experiment_assistant.experiments
ADD UNIQUE KEY uq_experiments_id_owner (id, user_id);
