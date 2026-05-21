"""Pull reference corpus from governance DB → parquet.

Runs the canonical SQL in data/v7-fhat/sql/ with window 2026-02-20..2026-03-20.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd
import psycopg2

REPO = pathlib.Path(__file__).resolve().parents[3]
SQL_DIR = REPO / "data" / "v7-fhat" / "sql"
OUT_DIR = REPO / "data" / "v7-fhat" / "corpus"


def _read_sql_template(path: pathlib.Path) -> str:
    raw = path.read_text()
    # psycopg2 does not expand psql-style :'name' binds. Strip them and build explicit params.
    return raw


def pull(window_start: str, window_end: str, dsn: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    state_sql_raw = _read_sql_template(SQL_DIR / "reference_state.sql")
    outcomes_sql_raw = _read_sql_template(SQL_DIR / "reference_outcomes.sql")

    # Replace :'window_start' / :'window_end' with %s placeholders.
    def rewrite(sql: str) -> str:
        return sql.replace(":'window_start'::timestamptz", "%s::timestamptz").replace(
            ":'window_end'::timestamptz", "%s::timestamptz"
        )

    state_sql = rewrite(state_sql_raw)
    outcomes_sql = rewrite(outcomes_sql_raw)

    with psycopg2.connect(dsn) as conn:
        print(f"[pull] state rows...", flush=True)
        with conn.cursor() as cur:
            cur.execute(state_sql, (window_start, window_end))
            cols = [d.name for d in cur.description]
            df_state = pd.DataFrame(cur.fetchall(), columns=cols)
        print(f"[pull]   got {len(df_state):,} state rows", flush=True)

        print(f"[pull] outcome rows...", flush=True)
        with conn.cursor() as cur:
            cur.execute(outcomes_sql, (window_start, window_end))
            cols = [d.name for d in cur.description]
            df_out = pd.DataFrame(cur.fetchall(), columns=cols)
        print(f"[pull]   got {len(df_out):,} outcome rows", flush=True)

    # Normalize object columns that pyarrow can't autoinfer (jsonb → str, array → list)
    for df in (df_state, df_out):
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].astype(str)

    # Save
    state_path = OUT_DIR / f"state_{window_start}_{window_end}.parquet"
    out_path = OUT_DIR / f"outcomes_{window_start}_{window_end}.parquet"
    df_state.to_parquet(state_path, index=False)
    df_out.to_parquet(out_path, index=False)
    print(f"[pull] wrote {state_path}")
    print(f"[pull] wrote {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-start", default="2026-02-20")
    ap.add_argument("--window-end", default="2026-03-20")
    ap.add_argument(
        "--dsn",
        default="postgresql://postgres:postgres@localhost:5432/governance",
    )
    args = ap.parse_args()
    pull(args.window_start, args.window_end, args.dsn)


if __name__ == "__main__":
    sys.exit(main())
