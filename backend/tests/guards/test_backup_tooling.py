from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BACKUP = (ROOT / "ops/backup/backup.sh").read_text(encoding="utf-8")
RESTORE = (ROOT / "ops/backup/restore.sh").read_text(encoding="utf-8")
CRON = (ROOT / "ops/backup/install-cron.sh").read_text(encoding="utf-8")
VERIFY = (ROOT / "deploy/lib/70_verify.sh").read_text(encoding="utf-8")
SECRETS = (ROOT / "deploy/lib/20_secrets.sh").read_text(encoding="utf-8")


def _body(source: str, function_name: str) -> str:
    start = source.index(f"{function_name}()")
    next_function = source.find("\nmain()", start)
    return source[start:] if next_function == -1 else source[start:next_function]


def test_successful_backup_encrypts_an_unique_gpg_artifact() -> None:
    assert "--encrypt" in BACKUP
    assert "--recipient \"$BACKUP_GPG_RECIPIENT\"" in BACKUP
    assert "basename=\"mysql-${timestamp}-${BASHPID}-${RANDOM}.sql.gpg\"" in BACKUP
    assert "[[ -s \"$encrypted\" ]]" in BACKUP


def test_successful_backup_writes_sha256_integrity_evidence() -> None:
    assert 'evidence="$staging/backup.sql.gpg.sha256"' in BACKUP
    assert 'sha256sum -- "$encrypted"' in BACKUP
    assert 'printf \'%s  %s\\n\' "$digest" "$basename" > "$evidence"' in BACKUP


def test_successful_backup_uploads_both_objects_to_r2() -> None:
    assert "r2_upload \"$encrypted\" \"$key\"" in BACKUP
    assert 'r2_upload "$evidence" "${key}.sha256"' in BACKUP
    assert 'aws s3 cp "$source" "s3://${R2_BUCKET}/${key}"' in BACKUP


def test_dump_failure_cannot_reach_upload_or_success() -> None:
    dump = BACKUP.index("if [[ -n \"${MYSQLDUMP_COMMAND:-}\" ]]")
    encryption = BACKUP.index("if [[ -n \"${GPG_ENCRYPT_COMMAND:-}\" ]]", dump)
    uploads = BACKUP.index('r2_upload "$encrypted"', encryption)
    assert "fail 'MySQL dump failed'" in BACKUP[dump:encryption]
    assert uploads > encryption
    assert "backup completed" in BACKUP


def test_gpg_failure_cannot_reach_upload_or_success() -> None:
    encryption = BACKUP.index("if [[ -n \"${GPG_ENCRYPT_COMMAND:-}\" ]]")
    digest = BACKUP.index('digest="$(sha256sum', encryption)
    assert "fail 'GPG encryption failed'" in BACKUP[encryption:digest]
    assert 'r2_upload "$encrypted"' not in BACKUP[encryption:digest]


def test_r2_failure_is_a_backup_failure() -> None:
    upload = BACKUP.index("r2_upload()")
    verify = BACKUP.index("r2_head()")
    assert "R2 upload failed" in BACKUP[upload:verify]
    assert "fail 'R2 upload failed'" in BACKUP[upload:verify]


def test_verify_rejects_local_sha256_mismatch() -> None:
    assert "local SHA-256 verification failed" in BACKUP
    assert 'actual="$(sha256sum -- "$archive"' in BACKUP
    assert "--verify" in BACKUP


def test_verify_is_read_only_and_does_not_upload_or_dump() -> None:
    verify_start = BACKUP.index("verify_backup()")
    create_start = BACKUP.index("create_backup()")
    verify_body = BACKUP[verify_start:create_start]
    assert "mysqldump" not in verify_body
    assert "GPG_ENCRYPT_COMMAND" not in verify_body
    assert "r2_upload" not in verify_body
    assert "r2_head" in verify_body
    assert "r2_download" in verify_body


def test_existing_backups_are_not_overwritten_or_deleted() -> None:
    assert "[[ ! -e \"$archive\" && ! -e \"${archive}.sha256\" ]]" in BACKUP
    assert "backup filename collision refused" in BACKUP
    assert "find \"$BACKUP_DIR\"" in BACKUP
    assert "rm -rf -- \"$BACKUP_DIR\"" not in BACKUP


def test_cron_uses_root_wrapper_without_weakening_secret_permissions() -> None:
    assert "stat -c '%a' \"$backup_conf\"" in CRON
    assert '== 600' in CRON
    assert "stat -c '%U' \"$backup_conf\"" in CRON
    assert "stat -c '%G' \"$backup_conf\"" in CRON
    assert "runuser --preserve-environment" in CRON
    assert "install --owner root --group root --mode 0700" in CRON
    assert "chmod" not in CRON


def test_restore_checks_sha256_before_decrypting() -> None:
    restore = _body(RESTORE, "restore_archive")
    assert restore.index('verify_digest "$archive" "$evidence"') < restore.index(
        "GPG_DECRYPT_COMMAND"
    )
    assert "SHA-256 verification failed; restore refused" in RESTORE


def test_restore_rejects_nonempty_target() -> None:
    assert "target database is not empty; restore refused" in RESTORE
    assert "SELECT COUNT(*) FROM information_schema.tables" in RESTORE
    assert "CONFIRM_EMPTY_TARGET" in RESTORE


def test_restore_has_no_automatic_data_erasure_path() -> None:
    assert "DROP" not in RESTORE.upper()
    assert "TRUNCATE" not in RESTORE.upper()
    assert "DELETE FROM" not in RESTORE.upper()


def test_backup_and_restore_do_not_print_secret_values() -> None:
    combined = f"{BACKUP}\n{RESTORE}\n{CRON}"
    assert "set -x" not in combined
    assert 'printf "%s" "$R2_' not in combined
    assert 'printf "%s" "$MYSQL_' not in combined
    assert "AWS_SECRET_ACCESS_KEY" in BACKUP
    assert "--only-show-errors" in BACKUP


def test_backup_verifier_fails_closed_on_missing_script_or_failed_verify() -> None:
    check_start = VERIFY.index("check_13_backup()")
    check_end = VERIFY.index("\n}\n", check_start) + 2
    check = VERIFY[check_start:check_end]
    assert '[[ -x "$backup_script" ]] || return 1' in check
    assert '[[ -s "$cron_path" ]] || return 1' in check
    assert '"$backup_script" --verify' in check
    assert "BACKUP_VERIFY_COMMAND" not in check


def test_deployment_secret_gate_requires_all_backup_r2_names() -> None:
    for name in (
        "BACKUP_GPG_RECIPIENT",
        "R2_ENDPOINT_URL",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
    ):
        assert f"require_name {name} \"$backup_conf\"" in SECRETS
