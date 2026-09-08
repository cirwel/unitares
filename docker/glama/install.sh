#!/bin/bash
# Shared by Dockerfile.glama and Glama's Debian trixie build-step runner.
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
# Prevent Debian package installation from starting a cluster during image build.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod +x /usr/sbin/policy-rc.d
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg build-essential git unzip tini redis-server erlang-base erlang-dev erlang-crypto erlang-inets erlang-public-key erlang-ssl erlang-tools erlang-parsetools erlang-syntax-tools erlang-runtime-tools erlang-xmerl erlang-asn1
install -d /usr/share/postgresql-common/pgdg
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main\n' > /etc/apt/sources.list.d/pgdg.list
# Never create the Debian default cluster, even at build time.
mkdir -p /etc/postgresql-common
printf 'create_main_cluster = false\n' > /etc/postgresql-common/createcluster.conf
apt-get update
apt-get install -y --no-install-recommends postgresql-18 postgresql-server-dev-18 flex bison
export PATH=/usr/lib/postgresql/18/bin:$PATH
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT
# Match the existing Compose extensions; version upgrades are a separate change.
git clone --depth 1 --branch PG18/v1.7.0-rc0 https://github.com/apache/age.git "$build_dir/age"
make -C "$build_dir/age" -j2
make -C "$build_dir/age" install
git clone --depth 1 --branch v0.8.3 https://github.com/pgvector/pgvector.git "$build_dir/vector"
make -C "$build_dir/vector" -j2 OPTFLAGS=""
make -C "$build_dir/vector" install
curl -fsSL https://github.com/elixir-lang/elixir/releases/download/v1.19.5/elixir-otp-27.zip -o "$build_dir/elixir.zip"
curl -fsSL https://github.com/elixir-lang/elixir/releases/download/v1.19.5/elixir-otp-27.zip.sha256sum -o "$build_dir/elixir.sha256"
expected=$(awk '{print $1}' "$build_dir/elixir.sha256")
printf '%s  %s\n' "$expected" "$build_dir/elixir.zip" | sha256sum -c -
unzip -q "$build_dir/elixir.zip" -d /opt/elixir
export PATH=/opt/elixir/bin:$PATH
export MIX_ENV=prod ERL_FLAGS='+S 2:2'
mix local.hex --force
mix local.rebar --force
(cd elixir/lease_plane && mix deps.get --only prod && mix deps.compile && mix compile)
# Use the production requirements, including asyncpg and redis, without the
# optional embedding model download. Never run plain `uv sync` at startup.
python -m venv /opt/unitares
/opt/unitares/bin/pip install --no-cache-dir -r requirements-docker.txt -c constraints.txt
useradd --system --home /data/unitares --uid 10001 unitares
# Glama clones tracked data/ examples; keep them out of live persistent state.
if [ -e /app/data ] || [ -L /app/data ]; then
    mv /app/data /app/bundled-data
fi
ln -s /data/unitares /app/data
mkdir -p /docker-entrypoint-initdb.d
cp -r db/postgres /docker-entrypoint-initdb.d/unitares
python - <<'PY'
import hashlib,pathlib
root=pathlib.Path('db/postgres')
h=hashlib.sha256()
for p in sorted(root.rglob('*')):
    if p.is_file() and (p.suffix=='.sql' or p.name=='docker-initdb.sh'):
        h.update(str(p.relative_to(root)).encode()+b'\0'+p.read_bytes())
pathlib.Path('/opt/unitares-schema.sha256').write_text(h.hexdigest())
PY
chmod -R a+rX /opt/elixir /app/elixir
rm -rf /var/lib/apt/lists/*
