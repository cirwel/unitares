FROM elixir:1.19-slim

# The image carries no .git, so the runtime `git rev-parse` in
# UnitaresLeasePlane.BuildInfo cannot resolve anything and /health would publish
# "unknown" forever -- leaving deploy-status stuck at HOT-RELOAD(?) for every
# containerised deployment. Bake the sha at build time instead of asking each
# operator to inject it. Mirrors #1792, which forwards UNITARES_BUILD_SHA
# through docker-compose for the Python server for the same reason.
#   docker build --build-arg UNITARES_BUILD_SHA=$(git rev-parse --short HEAD) .
ARG UNITARES_BUILD_SHA=unknown
ENV UNITARES_BUILD_SHA=${UNITARES_BUILD_SHA}

ENV MIX_ENV=prod

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && mix local.hex --force \
    && mix local.rebar --force

WORKDIR /app/elixir

# The lease plane consumes the sibling SDK through a path dependency.
COPY elixir/unitares_sdk ./unitares_sdk
COPY elixir/lease_plane/mix.exs elixir/lease_plane/mix.lock ./lease_plane/

WORKDIR /app/elixir/lease_plane
RUN mix deps.get --only prod && mix deps.compile

COPY elixir/lease_plane/config ./config
COPY elixir/lease_plane/lib ./lib
RUN mix compile

EXPOSE 8788

CMD ["mix", "run", "--no-halt"]
