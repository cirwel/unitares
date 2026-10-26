- **install:** the packaged Apple Silicon Homebrew CLI now gives native installs
  the coordinated `unitares update` lifecycle already expected from the Docker
  path: a read-only cached-tap `--check`, explicit package refresh, both writers
  stopped and verified, a private custom-format backup verified by `pg_restore`,
  four-formula pin management, full installed-formula inventory evidence around
  Homebrew dependency resolution, re-execution into the new keg, migrations,
  previous running/stopped-state restoration, identity-count verification,
  deep-health verification for restarted writers (schema verification when
  stopped), and a private journal resumed with `--resume`. Unfinished journals
  block `unitares start`; a safe pre-change `--abort` and a reviewed
  identity-count-mismatch acknowledgement provide explicit recovery paths.
  Verified backups are checksum-indexed, and `--cleanup-backups` provides a
  dry-run-by-default, journaled retention path that preserves the newest set and
  every update-journal reference. UNITARES never deletes Homebrew kegs.
  Direct `brew services` commands bypass the journal guard. Formula hooks still
  never migrate a database. Homebrew is recommended for supported Apple Silicon
  desktop Macs; Docker remains the cross-platform/server/CI path.
