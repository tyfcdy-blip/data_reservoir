CREATE TABLE managed_sources (
 dataset_slug TEXT PRIMARY KEY REFERENCES datasets(slug),
 title TEXT NOT NULL,
 definition JSONB NOT NULL,
 encrypted_secrets TEXT,
 approved_host TEXT NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT TRUE,
 schedule_enabled BOOLEAN NOT NULL DEFAULT FALSE,
 schedule_hour INTEGER NOT NULL CHECK(schedule_hour BETWEEN 0 AND 23),
 refresh_days INTEGER NOT NULL CHECK(refresh_days BETWEEN 1 AND 180),
 revision INTEGER NOT NULL CHECK(revision>0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE query_services (
 slug TEXT PRIMARY KEY,
 name TEXT NOT NULL,
 dataset_slug TEXT NOT NULL REFERENCES datasets(slug),
 definition JSONB NOT NULL,
 allowed_filter_fields JSONB NOT NULL,
 max_limit INTEGER NOT NULL CHECK(max_limit BETWEEN 1 AND 500),
 enabled BOOLEAN NOT NULL DEFAULT FALSE,
 revision INTEGER NOT NULL CHECK(revision>0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
