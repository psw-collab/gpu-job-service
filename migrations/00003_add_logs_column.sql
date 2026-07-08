-- +goose Up
-- +goose StatementBegin
ALTER TABLE jobs ADD COLUMN logs TEXT;
-- +goose StatementEnd


-- +goose Down
-- +goose StatementBegin
ALTER TABLE jobs DROP COLUMN logs;
-- +goose StatementEnd
