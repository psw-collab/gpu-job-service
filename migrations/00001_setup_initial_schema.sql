-- +goose Up
-- +goose StatementBegin
CREATE TABLE jobs (
    id VARCHAR(20) PRIMARY KEY,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    status_message TEXT,
    entrypoint VARCHAR(255) NOT NULL,
    entrypoint_content TEXT NOT NULL,
    requirements TEXT,
    python_version VARCHAR(10) NOT NULL DEFAULT '3.11',
    gpu_type VARCHAR(20) NOT NULL,
    gpu_count INTEGER NOT NULL DEFAULT 1,
    image_tag VARCHAR(500),
    requirements_hash VARCHAR(64),
    failure_reason VARCHAR(50),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
-- +goose StatementEnd


-- +goose Down
-- +goose StatementBegin
DROP TABLE IF EXISTS jobs;
-- +goose StatementEnd
