-- 仅增加机器密钥策略；SQL NULL 保留旧版整表授权行为，不改任何已有授权或业务数据。
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS policy JSONB;
