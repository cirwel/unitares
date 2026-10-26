- **install:** the packaged Apple Silicon Homebrew CLI now gives native installs
  the coordinated `unitares update` lifecycle already expected from the Docker
  path: a read-only cached-tap `--check`, explicit package refresh, both writers
  stopped and verified, a private custom-format backup verified by `pg_restore`,
  four-formula pin management, re-execution into the new keg, migrations,
  previous running/stopped-state restoration, identity-count and deep-health
  verification, and a private journal resumed with `--resume`. Formula hooks
  still never migrate a database. Homebrew is recommended for supported Apple
  Silicon desktop Macs; Docker remains the cross-platform/server/CI path.
