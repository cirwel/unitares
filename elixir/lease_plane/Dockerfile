FROM elixir:1.19-slim

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
