-- Design-only SQLite DDL. validate.py executes this only against :memory:.
CREATE TABLE layout_typed_state (
    kind TEXT NOT NULL CHECK (kind IN (
        'draft', 'session', 'capture', 'evidence', 'region_draft',
        'region_preview', 'adaptation_preview', 'rollback_preview',
        'package_import_preview', 'package_export', 'cursor',
        'review_revision', 'adaptation_asset', 'catalog', 'intent'
    )),
    state_key TEXT NOT NULL CHECK (
        length(state_key) = 64 AND state_key NOT GLOB '*[^0-9a-f]*'
    ),
    device_id TEXT,
    principal_id TEXT,
    revision INTEGER NOT NULL CHECK (
        typeof(revision) = 'integer' AND revision BETWEEN 1 AND 9007199254740991
    ),
    json TEXT NOT NULL CHECK (typeof(json) = 'text' AND length(json) > 0),
    PRIMARY KEY (kind, state_key)
);

CREATE INDEX layout_typed_state_owner
    ON layout_typed_state (device_id, principal_id, kind);
