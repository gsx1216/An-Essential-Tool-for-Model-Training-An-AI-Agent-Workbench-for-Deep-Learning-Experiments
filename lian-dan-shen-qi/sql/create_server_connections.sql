CREATE TABLE IF NOT EXISTS experiment_assistant.server_connections (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    name VARCHAR(100) NOT NULL,
    host VARCHAR(45) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    port INT UNSIGNED NOT NULL,
    username VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    auth_method VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    host_fingerprint VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    work_dir VARCHAR(1024) NOT NULL,
    encrypted_credentials MEDIUMTEXT CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_at DATETIME(6) NOT NULL,
    deleted_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_servers_identity_owner (id, user_id),
    KEY idx_servers_owner_created (user_id, created_at DESC, id DESC),
    CONSTRAINT fk_servers_user FOREIGN KEY (user_id) REFERENCES experiment_assistant.users (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_servers_port CHECK (port BETWEEN 1 AND 65535),
    CONSTRAINT ck_servers_auth CHECK (auth_method IN ('password', 'private_key')),
    CONSTRAINT ck_servers_secret CHECK ((deleted_at IS NULL AND OCTET_LENGTH(encrypted_credentials) BETWEEN 100 AND 131072)
        OR (deleted_at IS NOT NULL AND encrypted_credentials=''))
) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
