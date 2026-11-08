defmodule AgentOrchestrator.Profiles.Schema do
  @moduledoc """
  Pure profile JSON schema validation, request preparation, child environment
  construction, and secret resolution for `AgentOrchestrator.Profiles`.
  """

  @schema "unitares.orchestrator_profiles.v1"
  @default_inherit ~w(HOME PATH LANG TMPDIR)
  # Variables the runner provisions (agent_runner.ex). No profile may let a
  # caller set them.
  @provisioned ~w(UNITARES_SERVER_URL UNITARES_CLIENT_SESSION_ID UNITARES_ORCHESTRATED
                  UNITARES_PARENT_AGENT_ID UNITARES_SPAWN_REASON)
  @profile_keys ~w(cmd args cd env vars lineage server_url lease max_runtime_ms
                   max_output_lines max_concurrent max_per_hour)
  @env_keys ~w(inherit set secrets)
  @var_keys ~w(max_bytes pattern required)
  @request_keys ~w(profile vars max_runtime_ms max_output_lines lineage)
  @name_re ~r/\A[a-z0-9][a-z0-9_.-]{0,63}\z/
  @env_name_re ~r/\A[A-Za-z_][A-Za-z0-9_]{0,127}\z/
  # The runner's lineage check (agent_runner.ex), applied before any
  # reservation so a malformed parent is a schema refusal, never echoed.
  @uuid_re ~r/\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\z/
  @max_var_bytes 262_144
  @min_secret_bytes 4
  @spawn_reason_rule "lineage.spawn_reason must be 1-64 of A-Z a-z 0-9 _ . : -"

  @doc false
  def parse(json) when is_binary(json) do
    with {:ok, decoded} <- decode(json),
         :ok <- expect_keys(decoded, ~w(schema profiles), "file"),
         :ok <- expect(decoded["schema"] == @schema, "schema must be #{@schema}"),
         {:ok, raw_profiles} <- expect_map(decoded["profiles"], "profiles") do
      Enum.reduce_while(raw_profiles, {:ok, %{}}, fn {name, raw}, {:ok, acc} ->
        case parse_profile(name, raw) do
          {:ok, profile} -> {:cont, {:ok, Map.put(acc, name, profile)}}
          {:error, reason} -> {:halt, {:error, "profile #{safe_name(name)}: #{reason}"}}
        end
      end)
    end
  end

  # ---------- profile validation ----------

  defp parse_profile(name, raw) do
    with :ok <- expect(is_binary(name) and Regex.match?(@name_re, name), "invalid profile name"),
         {:ok, raw} <- expect_map(raw, "profile"),
         :ok <- expect_keys(raw, @profile_keys, "profile"),
         {:ok, cmd} <- abs_path(raw["cmd"], "cmd"),
         {:ok, args} <- string_list(Map.get(raw, "args", []), "args"),
         {:ok, cd} <- optional_abs_path(raw["cd"], "cd"),
         {:ok, env} <- parse_env(Map.get(raw, "env", %{})),
         {:ok, vars} <- parse_vars(Map.get(raw, "vars", %{})),
         :ok <- disjoint_names(env, vars),
         {:ok, lineage} <- one_of(Map.get(raw, "lineage", "none"), ~w(none caller), "lineage"),
         {:ok, server_url} <- optional_http_url(raw["server_url"]),
         {:ok, lease} <- parse_lease(raw["lease"]),
         {:ok, max_runtime} <- positive(raw["max_runtime_ms"], "max_runtime_ms"),
         {:ok, max_lines} <- positive(Map.get(raw, "max_output_lines", 1000), "max_output_lines"),
         {:ok, max_concurrent} <- positive(raw["max_concurrent"], "max_concurrent"),
         {:ok, max_per_hour} <- positive(raw["max_per_hour"], "max_per_hour") do
      profile = %{
        name: name,
        cmd: cmd,
        args: args,
        cd: cd,
        env: env,
        vars: vars,
        lineage: lineage,
        server_url: server_url,
        lease: lease,
        max_runtime_ms: max_runtime,
        max_output_lines: max_lines,
        max_concurrent: max_concurrent,
        max_per_hour: max_per_hour
      }

      {:ok, Map.put(profile, :digest, profile_digest(profile))}
    end
  end

  defp parse_env(raw) do
    with {:ok, raw} <- expect_map(raw, "env"),
         :ok <- expect_keys(raw, @env_keys, "env"),
         {:ok, inherit} <- string_list(Map.get(raw, "inherit", @default_inherit), "env.inherit"),
         :ok <- env_names(inherit, "env.inherit"),
         {:ok, set} <- string_map(Map.get(raw, "set", %{}), "env.set"),
         :ok <- env_names(Map.keys(set), "env.set"),
         {:ok, secrets} <- string_map(Map.get(raw, "secrets", %{}), "env.secrets"),
         :ok <- env_names(Map.keys(secrets) ++ Map.values(secrets), "env.secrets") do
      fixed = Map.keys(set) ++ Map.keys(secrets)

      cond do
        Enum.any?(fixed, &(&1 in inherit)) ->
          {:error, "env.set and env.secrets may not name an inherited variable"}

        not MapSet.disjoint?(MapSet.new(Map.keys(set)), MapSet.new(Map.keys(secrets))) ->
          {:error, "env.set and env.secrets name the same variable"}

        Enum.any?(fixed ++ inherit, &(&1 in @provisioned)) ->
          {:error, "env may not name a variable the orchestrator provisions"}

        true ->
          {:ok, %{inherit: Enum.uniq(inherit), set: set, secrets: secrets}}
      end
    end
  end

  defp parse_vars(raw) do
    with {:ok, raw} <- expect_map(raw, "vars"),
         :ok <- env_names(Map.keys(raw), "vars") do
      Enum.reduce_while(raw, {:ok, %{}}, fn {name, spec}, {:ok, acc} ->
        case parse_var(spec) do
          {:ok, var} -> {:cont, {:ok, Map.put(acc, name, var)}}
          {:error, reason} -> {:halt, {:error, "vars.#{name}: #{reason}"}}
        end
      end)
    end
  end

  defp parse_var(spec) do
    with {:ok, spec} <- expect_map(spec, "variable"),
         :ok <- expect_keys(spec, @var_keys, "variable"),
         {:ok, max_bytes} <- positive(spec["max_bytes"], "max_bytes"),
         :ok <- expect(max_bytes <= @max_var_bytes, "max_bytes is over #{@max_var_bytes}"),
         {:ok, required} <- boolean(Map.get(spec, "required", false), "required"),
         {:ok, pattern} <- compile_pattern(spec["pattern"]) do
      {:ok,
       %{max_bytes: max_bytes, required: required, pattern: pattern, source: spec["pattern"]}}
    end
  end

  defp compile_pattern(nil), do: {:ok, nil}

  defp compile_pattern(source) when is_binary(source) do
    case Regex.compile("\\A(?:" <> source <> ")\\z", "u") do
      {:ok, re} -> {:ok, re}
      {:error, _} -> {:error, "pattern does not compile"}
    end
  end

  defp compile_pattern(_), do: {:error, "pattern must be a string"}

  defp disjoint_names(env, vars) do
    taken = env.inherit ++ Map.keys(env.set) ++ Map.keys(env.secrets) ++ @provisioned

    case Enum.find(Map.keys(vars), &(&1 in taken)) do
      nil -> :ok
      name -> {:error, "vars.#{name} names an inherited, fixed, secret or provisioned variable"}
    end
  end

  defp parse_lease(nil), do: {:ok, :absent}
  defp parse_lease(false), do: {:ok, false}
  defp parse_lease(_), do: {:error, "lease must be false or absent"}

  # Everything that changes what runs; secrets are named by source variable, so
  # rotating a secret's value leaves the digest (and a keyed retry) unchanged.
  defp profile_digest(profile) do
    profile
    |> Map.update!(:vars, fn vars ->
      Map.new(vars, fn {k, v} -> {k, Map.delete(v, :pattern)} end)
    end)
    |> :erlang.term_to_binary([:deterministic])
    |> sha256()
  end

  # ---------- request preparation ----------

  @doc false
  def prepare(body, loaded_result, environ_fn) when is_map(body) do
    with :ok <- known_request_keys(body),
         {:ok, profiles} <- loaded_result,
         {:ok, name} <- request_profile(body),
         {:ok, profile} <- find_profile(profiles, name),
         {:ok, vars} <- request_vars(body["vars"], profile.vars),
         {:ok, max_runtime} <-
           ceiling(body["max_runtime_ms"], profile.max_runtime_ms, "max_runtime_ms"),
         {:ok, max_lines} <-
           ceiling(body["max_output_lines"], profile.max_output_lines, "max_output_lines"),
         {:ok, lineage} <- request_lineage(body["lineage"], profile.lineage) do
      env = child_env(profile, vars, environ_fn.())

      spec =
        %{
          cmd: profile.cmd,
          args: profile.args,
          env: env,
          stdin: "close",
          max_runtime_ms: max_runtime,
          max_output_lines: max_lines,
          profile: name
        }
        |> put_opt(:cd, profile.cd)
        |> put_opt(:server_url, profile.server_url)
        |> put_opt(:lineage, lineage)
        |> put_lease(profile.lease)

      digest =
        sha256(
          :erlang.term_to_binary(
            {:profile, name, Enum.sort(vars), max_runtime, max_lines, lineage, profile.digest},
            [:deterministic]
          )
        )

      {:ok, %{profile: name, spec: spec, digest: digest, revision: profile.digest}}
    end
  end

  def prepare(_body, _loaded_result, _environ_fn),
    do: {:error, :schema_invalid, "body must be a JSON object"}

  defp known_request_keys(body) do
    case Map.keys(body) -- @request_keys do
      [] -> :ok
      extra -> {:error, :schema_invalid, "unknown field(s): #{names(extra)}"}
    end
  end

  defp request_profile(body) do
    case body["profile"] do
      name when is_binary(name) and name != "" -> {:ok, name}
      _ -> {:error, :schema_invalid, "profile must be a non-empty string"}
    end
  end

  defp find_profile(profiles, name) do
    case Map.fetch(profiles, name) do
      {:ok, profile} -> {:ok, profile}
      :error -> {:error, :unknown_profile, "unknown profile #{safe_name(name)}"}
    end
  end

  @doc false
  def resolve_secrets(profile, getenv) do
    Enum.reduce_while(profile.env.secrets, {:ok, []}, fn {child, source}, {:ok, acc} ->
      case getenv.(source) do
        value when is_binary(value) and byte_size(value) >= @min_secret_bytes ->
          # Output is redacted line by line, so a value holding a line break
          # could leak in pieces; such a secret is refused, not half-redacted.
          if String.contains?(value, ["\n", "\r"]) do
            {:halt,
             {:error, :profile_unconfigured,
              "profile #{profile.name} is unconfigured: secret source #{source} holds a line break"}}
          else
            {:cont, {:ok, [{child, value} | acc]}}
          end

        _ ->
          {:halt,
           {:error, :profile_unconfigured,
            "profile #{profile.name} is unconfigured: secret source #{source} is unset"}}
      end
    end)
  end

  defp request_vars(nil, declared), do: request_vars(%{}, declared)

  defp request_vars(given, declared) when is_map(given) do
    with :ok <- known_vars(given, declared),
         :ok <- required_vars(given, declared) do
      Enum.reduce_while(given, {:ok, []}, fn {name, value}, {:ok, acc} ->
        spec = Map.fetch!(declared, name)

        cond do
          not is_binary(value) ->
            {:halt, {:error, :schema_invalid, "vars.#{name} must be a string"}}

          byte_size(value) > spec.max_bytes ->
            {:halt,
             {:error, :schema_invalid, "vars.#{name} is over its #{spec.max_bytes}-byte limit"}}

          not String.valid?(value) or String.contains?(value, <<0>>) ->
            {:halt, {:error, :schema_invalid, "vars.#{name} must be text without NUL"}}

          spec.pattern && not Regex.match?(spec.pattern, value) ->
            {:halt, {:error, :schema_invalid, "vars.#{name} does not match its pattern"}}

          true ->
            {:cont, {:ok, [{name, value} | acc]}}
        end
      end)
    end
  end

  defp request_vars(_given, _declared),
    do: {:error, :schema_invalid, "vars must be a JSON object"}

  defp known_vars(given, declared) do
    case Map.keys(given) -- Map.keys(declared) do
      [] -> :ok
      extra -> {:error, :schema_invalid, "unknown variable(s): #{names(extra)}"}
    end
  end

  defp required_vars(given, declared) do
    missing = for {name, %{required: true}} <- declared, not Map.has_key?(given, name), do: name

    case missing do
      [] ->
        :ok

      _ ->
        {:error, :schema_invalid,
         "missing required variable(s): #{Enum.join(Enum.sort(missing), ", ")}"}
    end
  end

  defp ceiling(nil, max, _field), do: {:ok, max}

  defp ceiling(n, max, _field) when is_integer(n) and n > 0 and n <= max, do: {:ok, n}

  defp ceiling(n, max, field) when is_integer(n) and n > max,
    do: {:error, :schema_invalid, "#{field} is over the profile's ceiling of #{max}"}

  defp ceiling(_n, _max, field),
    do: {:error, :schema_invalid, "#{field} must be a positive integer"}

  defp request_lineage(nil, _mode), do: {:ok, nil}

  defp request_lineage(_given, "none"),
    do: {:error, :schema_invalid, "lineage is not accepted by this profile"}

  defp request_lineage(%{} = given, "caller") do
    with :ok <- known_lineage_keys(given),
         {:ok, parent} <- uuid(given["parent_agent_uuid"], "lineage.parent_agent_uuid"),
         {:ok, reason} <- spawn_reason(given["spawn_reason"]) do
      base = %{parent_agent_uuid: parent}
      {:ok, if(reason, do: Map.put(base, :spawn_reason, reason), else: base)}
    end
  end

  defp request_lineage(_given, "caller"),
    do: {:error, :schema_invalid, "lineage must be a JSON object"}

  defp known_lineage_keys(given) do
    case Map.keys(given) -- ~w(parent_agent_uuid spawn_reason) do
      [] -> :ok
      extra -> {:error, :schema_invalid, "unknown lineage field(s): #{names(extra)}"}
    end
  end

  defp uuid(v, f) do
    if is_binary(v) and Regex.match?(@uuid_re, v),
      do: {:ok, v},
      else: {:error, :schema_invalid, "#{f} must be a UUID"}
  end

  defp spawn_reason(nil), do: {:ok, nil}

  defp spawn_reason(v) when is_binary(v) do
    if Regex.match?(~r/\A[A-Za-z0-9_.:-]{1,64}\z/, v),
      do: {:ok, v},
      else: {:error, :schema_invalid, @spawn_reason_rule}
  end

  defp spawn_reason(_v), do: {:error, :schema_invalid, @spawn_reason_rule}

  # Unset everything not inherited (or written here or as a secret), then
  # fixed values and the caller's variables. Secrets are resolved at admission
  # and reach the runner behind the spec's :secret_env closure; the runner
  # then adds its provisioned variables.
  defp child_env(profile, vars, environ) do
    keep = MapSet.new(profile.env.inherit)

    unset =
      for {name, _v} <- environ, not MapSet.member?(keep, name), do: {name, false}

    set = Enum.sort(profile.env.set)
    written = MapSet.new(Map.keys(profile.env.secrets) ++ Enum.map(set ++ vars, &elem(&1, 0)))

    Enum.reject(unset, fn {k, _} -> MapSet.member?(written, k) end) ++
      set ++ Enum.sort(vars)
  end

  # ---------- helpers ----------

  defp decode(json) do
    case Jason.decode(json) do
      {:ok, %{} = map} -> {:ok, map}
      {:ok, _} -> {:error, "file must hold a JSON object"}
      {:error, _} -> {:error, "file is not valid JSON"}
    end
  end

  defp expect(true, _reason), do: :ok
  defp expect(_false, reason), do: {:error, reason}

  defp expect_map(%{} = m, _what), do: {:ok, m}
  defp expect_map(_v, what), do: {:error, "#{what} must be a JSON object"}

  defp expect_keys(map, allowed, what) do
    case Map.keys(map) -- allowed do
      [] -> :ok
      extra -> {:error, "unknown #{what} key(s): #{names(extra)}"}
    end
  end

  defp abs_path(v, field) when is_binary(v) do
    if Path.type(v) == :absolute and not String.contains?(v, <<0>>),
      do: {:ok, v},
      else: {:error, "#{field} must be an absolute path"}
  end

  defp abs_path(_v, field), do: {:error, "#{field} must be an absolute path"}

  defp optional_abs_path(nil, _field), do: {:ok, nil}
  defp optional_abs_path(v, field), do: abs_path(v, field)

  defp string_list(list, field) when is_list(list) do
    if Enum.all?(list, &is_binary/1),
      do: {:ok, list},
      else: {:error, "#{field} must be a list of strings"}
  end

  defp string_list(_v, field), do: {:error, "#{field} must be a list of strings"}

  defp string_map(%{} = m, field) do
    if Enum.all?(m, fn {k, v} -> is_binary(k) and is_binary(v) end),
      do: {:ok, m},
      else: {:error, "#{field} must map strings to strings"}
  end

  defp string_map(_v, field), do: {:error, "#{field} must map strings to strings"}

  defp env_names(list, field) do
    if Enum.all?(list, &(is_binary(&1) and Regex.match?(@env_name_re, &1))),
      do: :ok,
      else: {:error, "#{field} holds an invalid variable name"}
  end

  defp one_of(v, allowed, field) do
    if v in allowed,
      do: {:ok, v},
      else: {:error, "#{field} must be one of #{Enum.join(allowed, ", ")}"}
  end

  # The runner accepts only http(s) server URLs; refusing others at load keeps
  # a bad file from replacing the last good set.
  defp optional_http_url(nil), do: {:ok, nil}

  defp optional_http_url(v) when is_binary(v) do
    if String.starts_with?(v, ["http://", "https://"]) and byte_size(v) > 8,
      do: {:ok, v},
      else: {:error, "server_url must be an http:// or https:// URL"}
  end

  defp optional_http_url(_v), do: {:error, "server_url must be an http:// or https:// URL"}

  defp positive(n, _field) when is_integer(n) and n > 0, do: {:ok, n}
  defp positive(_n, field), do: {:error, "#{field} must be a positive integer"}

  defp boolean(b, _field) when is_boolean(b), do: {:ok, b}
  defp boolean(_b, field), do: {:error, "#{field} must be true or false"}

  defp put_opt(spec, _key, nil), do: spec
  defp put_opt(spec, key, value), do: Map.put(spec, key, value)

  defp put_lease(spec, :absent), do: spec
  defp put_lease(spec, false), do: Map.put(spec, :lease, false)

  # Field and variable names a caller sent are echoed only when they look like
  # names; anything else is summarised, so a refusal never carries a value.
  defp names(list) do
    list
    |> Enum.map(fn n ->
      if is_binary(n) and byte_size(n) <= 64 and n =~ ~r/\A[\w.-]+\z/,
        do: n,
        else: "(invalid name)"
    end)
    |> Enum.sort()
    |> Enum.join(", ")
  end

  @doc false
  def safe_name(n) when is_binary(n) do
    if Regex.match?(@name_re, n), do: n, else: "(invalid name)"
  end

  def safe_name(_n), do: "(invalid name)"

  @doc false
  def sha256(bin), do: :crypto.hash(:sha256, bin) |> Base.encode16(case: :lower)
end
