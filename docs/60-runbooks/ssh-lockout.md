# SSH lockout prevention and rollback

`deploy/lib/10_system.sh` controls SSH hardening through the inventory key
`harden_ssh`, which defaults to `true`. Before changing anything it requires:

1. The current SSH session has a non-empty, parseable `authorized_keys` file.
2. The current session appears in the recent SSH/journald authentication log as
   `Accepted publickey` from the current source address.

If either check cannot be proven, the script stops before changing SSH. It
never disables password authentication blindly.

When the checks pass, the script backs up `/etc/ssh/sshd_config` to a
timestamped `/etc/ssh/sshd_config.lingsway.*.bak`, applies:

```text
PermitRootLogin prohibit-password
PasswordAuthentication no
```

It runs `sshd -t` before installation, reloads `ssh` rather than restarting
it, and verifies the effective `sshd -T` values. A failed validation or reload
restores the timestamped backup and reloads the previous configuration.

After deployment, keep the existing key session open. From a second terminal,
verify a new key-based SSH connection before closing the first one. A
password-only attempt must be rejected. If access is lost, use the provider's
console, restore the exact timestamped backup, run `sshd -t`, and reload the
SSH service before retrying.

Setting `harden_ssh: false` is an explicit exception: deployment leaves SSH
unchanged and verifier item 8 is reported as `SKIPPED`, never `PASS`. Production
should use the default `true` after the key-login preflight is satisfied.
