"""
Met Museum Crawler — Colab/Cloud version.
Run this in Google Colab or any cloud VM to test if the rate limit is different.

Quick test (run first to check rate limit):
    !pip install aiohttp
    !python crawl_colab.py --limit 500 --test-rate

Full crawl:
    !python crawl_colab.py

Download result:
    from google.colab import files
    files.download('raw_artworks.db')
"""

import asyncio
import aiohttp
import sqlite3
import json
import csv
import time
import argparse
import os
import io

MET_API = "https://collectionapi.metmuseum.org/public/collection/v1"
MET_CSV_URL = "https://media.githubusercontent.com/media/metmuseum/openaccess/master/MetObjects.csv"
DB_PATH = os.environ.get("CRAWL_DB_PATH", "raw_artworks.db")

BURST_SIZE = 80
BURST_COOLDOWN = 60  # will be auto-tuned if --test-rate is used
COMMIT_EVERY = 500
MAX_RETRIES = 3


def log(msg):
    print(msg, flush=True)


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_artworks (
            met_object_id INTEGER PRIMARY KEY,
            data TEXT,
            is_public_domain BOOLEAN,
            has_image BOOLEAN,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def get_existing_ids(conn):
    cursor = conn.execute("SELECT met_object_id FROM raw_artworks")
    return {row[0] for row in cursor.fetchall()}


async def get_public_domain_ids_from_csv(session):
    log("Downloading Met Museum CSV from GitHub...")
    async with session.get(MET_CSV_URL) as resp:
        resp.raise_for_status()
        text = await resp.text()

    log("Parsing CSV...")
    reader = csv.DictReader(io.StringIO(text))
    public_ids = []
    total = 0
    for row in reader:
        total += 1
        if row.get("Is Public Domain", "").strip().lower() == "true":
            try:
                public_ids.append(int(row["Object ID"]))
            except (ValueError, KeyError):
                pass

    log(f"CSV: {total} total objects, {len(public_ids)} public domain")
    return public_ids


async def fetch_object(session, object_id):
    url = f"{MET_API}/objects/{object_id}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                is_public = data.get("isPublicDomain", False)
                has_image = bool(data.get("primaryImageSmall"))
                return object_id, json.dumps(data), is_public, has_image, "ok"
            elif resp.status == 404:
                return object_id, None, False, False, "404"
            elif resp.status in (429, 403):
                return object_id, None, None, None, "throttled"
            else:
                return None, None, None, None, f"http:{resp.status}"
    except Exception as e:
        return None, None, None, None, f"exc:{e}"


async def test_rate_limit(session, ids):
    """Test the rate limit on this machine. Returns optimal burst size and cooldown."""
    log("\n=== Rate Limit Test ===")
    log("Testing burst sizes to find the limit on this IP...\n")

    # Test increasingly large bursts
    offset = 0
    for burst_size in [80, 160, 320, 500, 1000]:
        if offset + burst_size > len(ids):
            break

        batch = ids[offset:offset + burst_size]
        start = time.time()
        results = await asyncio.gather(*[fetch_object(session, oid) for oid in batch])
        elapsed = time.time() - start

        ok = sum(1 for _, _, _, _, s in results if s == "ok")
        throttled = sum(1 for _, _, _, _, s in results if s == "throttled")

        log(f"  Burst {burst_size}: ok={ok} throttled={throttled} ({elapsed:.1f}s, {burst_size/elapsed:.0f} req/s)")

        if throttled > 0:
            log(f"\n  Rate limit hit at burst size {burst_size}. Safe burst: {ok}")
            safe_burst = max(ok - 10, 20)  # leave margin

            # Test cooldown needed
            log(f"\n  Testing cooldown (waiting 10s, then another burst of {safe_burst})...")
            for cooldown in [10, 20, 30, 45, 60]:
                await asyncio.sleep(cooldown)
                offset += burst_size
                test_batch = ids[offset:offset + safe_burst]
                test_results = await asyncio.gather(*[fetch_object(session, oid) for oid in test_batch])
                test_ok = sum(1 for _, _, _, _, s in test_results if s == "ok")
                test_throttled = sum(1 for _, _, _, _, s in test_results if s == "throttled")
                log(f"    After {cooldown}s cooldown: ok={test_ok} throttled={test_throttled}")
                if test_throttled == 0:
                    log(f"\n  === Optimal settings: burst={safe_burst}, cooldown={cooldown}s ===")
                    log(f"  Sustained rate: {safe_burst/cooldown:.1f} req/s")
                    log(f"  ETA for 248K objects: {248472/safe_burst*cooldown/3600:.1f} hours")
                    return safe_burst, cooldown

            log(f"\n  Could not find good cooldown. Using conservative: burst={safe_burst}, cooldown=60s")
            return safe_burst, 60

        offset += burst_size
        await asyncio.sleep(5)  # brief pause between test bursts

    log(f"\n  No throttling up to burst {burst_size}! This IP has generous limits.")
    log(f"  Using burst={burst_size}, cooldown=2s")
    return burst_size, 2


async def crawl(db_path=DB_PATH, limit=None, do_rate_test=False):
    global BURST_SIZE, BURST_COOLDOWN

    conn = init_db(db_path)
    existing_ids = get_existing_ids(conn)
    log(f"Already in DB: {len(existing_ids)}")

    async with aiohttp.ClientSession() as session:
        candidate_ids = await get_public_domain_ids_from_csv(session)

        all_missing = [oid for oid in candidate_ids if oid not in existing_ids]
        missing_ids = all_missing[:limit] if limit else all_missing
        log(f"Missing: {len(all_missing)} | To fetch this run: {len(missing_ids)}")

        if not missing_ids:
            log("Nothing to fetch.")
            conn.close()
            return

        # Auto-tune rate limit if requested
        if do_rate_test:
            BURST_SIZE, BURST_COOLDOWN = await test_rate_limit(session, missing_ids)
            # Re-filter since rate test consumed some IDs
            existing_ids = get_existing_ids(conn)
            all_missing = [oid for oid in candidate_ids if oid not in existing_ids]
            missing_ids = all_missing[:limit] if limit else all_missing
            log(f"\nAfter rate test — Missing: {len(all_missing)} | To fetch: {len(missing_ids)}")

        stats = {"valid": 0, "invalid": 0, "not_found": 0, "throttled": 0, "errors": 0}
        pending_inserts = []
        error_samples = []
        completed = 0
        start_time = time.time()
        total = len(missing_ids)

        retry_counts = {}
        retry_ids = []
        i = 0

        while i < total or retry_ids:
            burst = []
            while retry_ids and len(burst) < BURST_SIZE:
                burst.append(retry_ids.pop(0))
            while i < total and len(burst) < BURST_SIZE:
                burst.append(missing_ids[i])
                i += 1

            if not burst:
                break

            results = await asyncio.gather(*[fetch_object(session, oid) for oid in burst])

            batch_throttled = 0
            for object_id, data_json, is_public, has_image, status in results:
                if status == "ok":
                    if is_public and has_image:
                        stats["valid"] += 1
                    else:
                        stats["invalid"] += 1
                    pending_inserts.append((object_id, data_json, is_public, has_image))
                    completed += 1
                    retry_counts.pop(object_id, None)
                elif status == "404":
                    stats["not_found"] += 1
                    pending_inserts.append((object_id, None, False, False))
                    completed += 1
                elif status == "throttled":
                    stats["throttled"] += 1
                    batch_throttled += 1
                    count = retry_counts.get(object_id, 0) + 1
                    if count <= MAX_RETRIES:
                        retry_counts[object_id] = count
                        retry_ids.append(object_id)
                    else:
                        stats["errors"] += 1
                        completed += 1
                        retry_counts.pop(object_id, None)
                else:
                    stats["errors"] += 1
                    if len(error_samples) < 5:
                        error_samples.append(status)
                    completed += 1

            if len(pending_inserts) >= COMMIT_EVERY:
                conn.executemany(
                    "INSERT OR IGNORE INTO raw_artworks (met_object_id, data, is_public_domain, has_image) VALUES (?, ?, ?, ?)",
                    pending_inserts,
                )
                conn.commit()
                pending_inserts = []

            elapsed = time.time() - start_time
            rate = completed / elapsed if elapsed > 0 else 0
            remaining = total - completed
            eta_h = (remaining / rate / 3600) if rate > 0 else 0
            log(
                f"  [{completed}/{total}] "
                f"{rate:.1f} req/s | "
                f"ETA {eta_h:.1f}h | "
                f"valid={stats['valid']} invalid={stats['invalid']} "
                f"404={stats['not_found']} throttled={stats['throttled']} err={stats['errors']}"
                + (f" retry_queue={len(retry_ids)}" if retry_ids else "")
            )

            if batch_throttled > 0:
                cooldown = BURST_COOLDOWN * 2
                log(f"  Throttled ({batch_throttled}/{len(burst)}), cooling down {cooldown}s...")
                await asyncio.sleep(cooldown)
            else:
                await asyncio.sleep(BURST_COOLDOWN)

        if pending_inserts:
            conn.executemany(
                "INSERT OR IGNORE INTO raw_artworks (met_object_id, data, is_public_domain, has_image) VALUES (?, ?, ?, ?)",
                pending_inserts,
            )
            conn.commit()

    elapsed = time.time() - start_time
    log(f"\nDone in {elapsed/3600:.1f} hours ({elapsed/60:.0f} minutes)")
    log(f"  Fetched: {completed}")
    log(f"  Valid (public domain + image): {stats['valid']}")
    log(f"  Invalid: {stats['invalid']}")
    log(f"  404: {stats['not_found']}")
    log(f"  Throttled: {stats['throttled']}")
    log(f"  Errors: {stats['errors']}")
    if error_samples:
        log(f"  Sample errors: {error_samples}")

    total_in_db = conn.execute("SELECT COUNT(*) FROM raw_artworks").fetchone()[0]
    valid_in_db = conn.execute(
        "SELECT COUNT(*) FROM raw_artworks WHERE is_public_domain = 1 AND has_image = 1"
    ).fetchone()[0]
    log(f"\nDB totals: {total_in_db} rows, {valid_in_db} valid")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Crawl Met Museum API")
    parser.add_argument("--limit", type=int, help="Max objects to fetch (for testing)")
    parser.add_argument("--db", default=DB_PATH, help="Path to output database")
    parser.add_argument("--test-rate", action="store_true", help="Auto-detect rate limit before crawling")
    args = parser.parse_args()
    asyncio.run(crawl(db_path=args.db, limit=args.limit, do_rate_test=args.test_rate))


if __name__ == "__main__":
    main()
