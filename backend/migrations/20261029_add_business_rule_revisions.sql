CREATE TABLE business_rule_revisions (
    revision_id CHAR(36) NOT NULL,
    base_revision_id CHAR(36) NULL,
    base_rulebook_hash CHAR(64) NOT NULL,
    document_markdown MEDIUMTEXT NOT NULL,
    compiled_rulebook JSON NOT NULL,
    rulebook_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_by VARCHAR(32) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP NULL DEFAULT NULL,
    PRIMARY KEY (revision_id),
    KEY idx_business_rule_revisions_status_created (status, created_at),
    CONSTRAINT chk_business_rule_revision_status
        CHECK (status IN ('draft', 'published', 'superseded'))
);

CREATE TABLE business_rule_active (
    state_id TINYINT NOT NULL,
    active_revision_id CHAR(36) NULL,
    PRIMARY KEY (state_id),
    CONSTRAINT chk_business_rule_active_singleton CHECK (state_id = 1),
    CONSTRAINT fk_business_rule_active_revision
        FOREIGN KEY (active_revision_id)
        REFERENCES business_rule_revisions (revision_id)
);

INSERT INTO business_rule_active (state_id, active_revision_id)
VALUES (1, NULL);
