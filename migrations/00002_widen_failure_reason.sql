-- +goose Up
-- +goose StatementBegin
ALTER TABLE jobs ALTER COLUMN failure_reason TYPE TEXT;
-- +goose StatementEnd


-- +goose Down
-- +goose StatementBegin
ALTER TABLE jobs ALTER COLUMN failure_reason TYPE VARCHAR(50);
-- +goose StatementEnd
