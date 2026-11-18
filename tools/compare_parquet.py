"""Compare the Parquet files of two folders row by row: for example the tables of the page's build by tiles (e2e.py --tiles)
and those of `duckosm-rust tiles` at the same zoom. The order of the rows is not compared, and the columns of the time of the
build (built_at, created, created_at) are not compared. Prints one line for each file and exits with 1 when a file differs or
is in one folder only.

    python tools/compare_parquet.py <folder 1> <folder 2> [--skip name,name]
"""
import argparse
import pathlib

import duckdb

a = argparse.ArgumentParser()
a.add_argument("one")
a.add_argument("two")
a.add_argument("--skip", default="", help="file names (without .parquet) not to compare")
args = a.parse_args()
one, two = pathlib.Path(args.one), pathlib.Path(args.two)
skip = set(filter(None, args.skip.split(",")))
names = lambda d: {p.stem for p in d.glob("*.parquet")} - skip  # noqa: E731
con = duckdb.connect()
bad = 0
for name in sorted(names(one) | names(two)):
    if name not in names(one) or name not in names(two):
        bad += 1
        print(f"ONLY {name}: only in {one if name in names(one) else two}")
        continue
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{one / name}.parquet')").fetchall()]
    keep = ", ".join(f'"{c}"' for c in cols if c not in ("built_at", "created", "created_at")) or "1"
    # each row as text: the types with no order (MAP) compare too
    q = lambda d: f"SELECT ({keep})::VARCHAR AS r FROM read_parquet('{d / name}.parquet')"  # noqa: E731
    n1, n2 = (con.execute(f"SELECT count(*) FROM read_parquet('{d / name}.parquet')").fetchone()[0] for d in (one, two))
    only1 = con.execute(f"SELECT count(*) FROM ({q(one)} EXCEPT ALL {q(two)})").fetchone()[0]
    only2 = con.execute(f"SELECT count(*) FROM ({q(two)} EXCEPT ALL {q(one)})").fetchone()[0]
    ok = only1 == 0 and only2 == 0
    bad += not ok
    print(f"{'ok  ' if ok else 'DIFF'} {name}: {n1} rows" + ("" if ok else f" and {n2}; {only1} only in {one}, {only2} only in {two}"))
raise SystemExit(1 if bad else 0)
