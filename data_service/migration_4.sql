-- 仅新增管理配置，不改变任何来源、查询模板或业务数据。
CREATE TABLE llm_settings (
    id smallint PRIMARY KEY CHECK (id = 1),
    base_url text NOT NULL,
    model text NOT NULL,
    encrypted_api_key text,
    enabled boolean NOT NULL DEFAULT false,
    timeout_seconds integer NOT NULL CHECK (timeout_seconds BETWEEN 5 AND 90),
    token_parameter text NOT NULL CHECK (token_parameter IN ('max_tokens','max_completion_tokens')),
    revision integer NOT NULL CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
