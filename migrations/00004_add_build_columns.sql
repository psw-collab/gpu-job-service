-- +goose Up
-- +goose StatementBegin
ALTER TABLE jobs ADD COLUMN source_archive BYTEA;
-- +goose StatementEnd
-- +goose StatementBegin
ALTER TABLE jobs ADD COLUMN scheduled_at TIMESTAMPTZ;
-- +goose StatementEnd
-- +goose StatementBegin
-- Multi-file submissions carry their entrypoint inside source_archive
-- instead of as raw text, so this can no longer be required.
ALTER TABLE jobs ALTER COLUMN entrypoint_content DROP NOT NULL;
-- +goose StatementEnd


-- +goose Down
-- +goose StatementBegin
ALTER TABLE jobs DROP COLUMN source_archive;
-- +goose StatementEnd
-- +goose StatementBegin
ALTER TABLE jobs DROP COLUMN scheduled_at;
-- +goose StatementEnd
-- +goose StatementBegin
ALTER TABLE jobs ALTER COLUMN entrypoint_content SET NOT NULL;
-- +goose StatementEnd
