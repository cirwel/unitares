import Config

config :lease_plane,
  database_url:
    System.get_env("UNITARES_LEASE_PLANE_DATABASE_URL") ||
      "postgresql://postgres:postgres@localhost:5432/governance",
  pool_size: 4

if File.exists?("config/#{config_env()}.exs") do
  import_config "#{config_env()}.exs"
end
