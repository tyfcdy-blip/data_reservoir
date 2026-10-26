CREATE TABLE schema_versions(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE auth_admin(id SMALLINT PRIMARY KEY CHECK(id=1),username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE auth_sessions(token_hash TEXT PRIMARY KEY,admin_id SMALLINT NOT NULL REFERENCES auth_admin(id) ON DELETE CASCADE,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),expires_at TIMESTAMPTZ NOT NULL);
CREATE INDEX auth_sessions_expiry_idx ON auth_sessions(expires_at);
CREATE TABLE auth_login_limits(bucket_key TEXT PRIMARY KEY,window_start TIMESTAMPTZ NOT NULL,attempts INTEGER NOT NULL CHECK(attempts>=0));
CREATE TABLE datasets(slug TEXT PRIMARY KEY,name TEXT NOT NULL,table_name TEXT NOT NULL UNIQUE,definition JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE api_keys(id UUID PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,token_hint TEXT NOT NULL,grants JSONB NOT NULL,requests_per_minute INTEGER NOT NULL CHECK(requests_per_minute BETWEEN 1 AND 600),created_at TIMESTAMPTZ NOT NULL DEFAULT now(),expires_at TIMESTAMPTZ NOT NULL,revoked_at TIMESTAMPTZ);
CREATE TABLE api_key_limits(key_id UUID PRIMARY KEY REFERENCES api_keys(id) ON DELETE CASCADE,window_start TIMESTAMPTZ NOT NULL,attempts INTEGER NOT NULL);
CREATE TABLE batches(
 id UUID PRIMARY KEY,dataset_slug TEXT NOT NULL REFERENCES datasets(slug),business_date DATE NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('pull','push')),
 status TEXT NOT NULL CHECK(status IN ('pending','running','uploading','succeeded','failed','cancelled')),
 owner_key_id UUID REFERENCES api_keys(id),expected_rows BIGINT CHECK(expected_rows BETWEEN 0 AND 1000000),
 row_count BIGINT NOT NULL DEFAULT 0 CHECK(row_count>=0),page_count INTEGER NOT NULL DEFAULT 0 CHECK(page_count>=0),
 idempotency_key TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 started_at TIMESTAMPTZ,finished_at TIMESTAMPTZ,error_code TEXT,error_message TEXT,
 UNIQUE(dataset_slug,idempotency_key)
);
CREATE UNIQUE INDEX batches_active_day_idx ON batches(dataset_slug,business_date) WHERE status IN ('pending','running','uploading');
CREATE INDEX batches_queue_idx ON batches(status,created_at);
CREATE INDEX batches_dataset_date_idx ON batches(dataset_slug,business_date);
CREATE TABLE batch_chunks(batch_id UUID NOT NULL REFERENCES batches(id) ON DELETE CASCADE,chunk_index INTEGER NOT NULL CHECK(chunk_index>=0),digest TEXT NOT NULL,row_count INTEGER NOT NULL CHECK(row_count>=0),PRIMARY KEY(batch_id,chunk_index));
CREATE TABLE published_days(dataset_slug TEXT NOT NULL REFERENCES datasets(slug),business_date DATE NOT NULL,batch_id UUID NOT NULL REFERENCES batches(id),row_count BIGINT NOT NULL CHECK(row_count>=0),published_at TIMESTAMPTZ NOT NULL DEFAULT now(),PRIMARY KEY(dataset_slug,business_date));
CREATE TABLE worker_heartbeat(worker_key TEXT PRIMARY KEY,heartbeat_at TIMESTAMPTZ NOT NULL,status TEXT NOT NULL);
