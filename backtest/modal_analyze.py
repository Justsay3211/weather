"""Standalone Modal script — weather market price calibration from REAL data."""
import modal

app = modal.App("weather-analyze")
vol = modal.Volume.from_name("pm-data", create_if_missing=True)
image = modal.Image.debian_slim().pip_install("duckdb", "huggingface_hub")


@app.function(image=image, volumes={"/data": vol}, timeout=1200)
def calibrate():
    import duckdb

    con = duckdb.connect()
    m = "/data/markets.parquet"
    q = "/data/quant.parquet"

    n_w = con.execute(
        f"SELECT count(*) FROM '{m}' "
        f"WHERE slug LIKE 'highest-temperature-in-%' "
        f"OR slug LIKE 'lowest-temperature-in-%'"
    ).fetchone()[0]
    print(f"Weather markets in dataset: {n_w}")

    n_c = con.execute(
        f"SELECT count(*) FROM '{m}' "
        f"WHERE (slug LIKE 'highest-temperature-in-%' OR slug LIKE 'lowest-temperature-in-%') "
        f"AND closed = 1 AND volume > 0"
    ).fetchone()[0]
    print(f"Closed weather markets with volume: {n_c}")

    # The key question: at each price level, what's the real win rate?
    try:
        rows = con.execute(f"""
            WITH wm AS (
                SELECT id, CAST(outcome_prices AS VARCHAR) as op
                FROM '{m}'
                WHERE (slug LIKE 'highest-temperature-in-%'
                       OR slug LIKE 'lowest-temperature-in-%')
                  AND closed = 1
            ),
            tw AS (
                SELECT q.price,
                       CASE WHEN wm.op LIKE '[''1''%' THEN 1 ELSE 0 END as yes
                FROM '{q}' q
                JOIN wm ON CAST(q.market_id AS VARCHAR) = CAST(wm.id AS VARCHAR)
                WHERE q.price > 0.001 AND q.price < 0.999
            )
            SELECT
                CASE
                    WHEN price < 0.01 THEN '<1c'
                    WHEN price < 0.03 THEN '1-3c'
                    WHEN price < 0.05 THEN '3-5c'
                    WHEN price < 0.08 THEN '5-8c'
                    WHEN price < 0.12 THEN '8-12c'
                    WHEN price < 0.18 THEN '12-18c'
                    WHEN price < 0.25 THEN '18-25c'
                    WHEN price < 0.35 THEN '25-35c'
                    WHEN price < 0.50 THEN '35-50c'
                    WHEN price < 0.70 THEN '50-70c'
                    ELSE '70c+'
                END as tier,
                count(*) as n,
                round(avg(price), 4) as avg_px,
                round(avg(yes), 4) as win_rate,
                round(avg(yes) - avg(price), 4) as edge
            FROM tw
            GROUP BY 1
            HAVING count(*) >= 20
            ORDER BY avg(price)
        """).fetchall()

        print("\n" + "=" * 70)
        print("REAL PRICE CALIBRATION — Weather Markets (SII-WANGZJ dataset)")
        print("=" * 70)
        print(f"{'Price Tier':>10} | {'Trades':>7} | {'Avg Price':>9} | "
              f"{'Win Rate':>9} | {'Edge':>8} | Verdict")
        print("-" * 70)
        for r in rows:
            tier, n, px, wr, edge = r
            v = "STRONG" if edge > 0.05 else ("OK" if edge > 0.02 else ("weak" if edge > 0 else "LOSS"))
            print(f"{tier:>10} | {n:7,d} | ${px:8.4f} | {wr:8.1%} | {edge:+7.1%} | {v}")
        print("-" * 70)
        print("Reading: 'Edge > 0' means the token wins more often than its price.")
        print("Buying where Edge > 0.05 = positive expected value after spread + fees.")
    except Exception as e:
        print(f"Calibration error: {e}")

    # City analysis
    print("\n--- City Win Rates ---")
    try:
        cities = con.execute(f"""
            SELECT
                regexp_extract(slug, 'temperature-in-([a-z-]+)-on-', 1) as city,
                count(*) as n,
                sum(volume) as vol,
                avg(CASE WHEN CAST(outcome_prices AS VARCHAR) LIKE '[''1''%'
                    THEN 1.0 ELSE 0.0 END) as yes_rate
            FROM '{m}'
            WHERE (slug LIKE 'highest-temperature-in-%'
                   OR slug LIKE 'lowest-temperature-in-%')
              AND closed = 1 AND volume > 0
            GROUP BY city
            HAVING count(*) >= 5
            ORDER BY n DESC
        """).fetchall()
        print(f"{'City':15} | {'Markets':>7} | {'Volume':>10} | {'Yes Rate':>9}")
        for r in cities[:20]:
            print(f"{r[0] or '?':15} | {r[1]:7d} | ${r[2]:9,.0f} | {r[3]:8.1%}")
    except Exception as e:
        print(f"City error: {e}")

    print("\nDone.")
