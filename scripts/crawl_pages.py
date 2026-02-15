import asyncio
import aiohttp
import sqlite3
import re
import time
import argparse
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DB_PATH = os.path.join(SCRIPT_DIR, "../data/raw_artworks.db")
PAGES_DB_PATH = os.path.join(SCRIPT_DIR, "../data/met_pages.db")

MET_BASE = "https://www.metmuseum.org/art/collection/search"
CONCURRENCY = 75
COMMIT_EVERY = 500
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def log(msg):
    print(msg, flush=True)


def init_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_content (
            met_object_id INTEGER PRIMARY KEY,
            description TEXT,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def get_existing_ids(conn):
    cursor = conn.execute("SELECT met_object_id FROM page_content")
    return {row[0] for row in cursor.fetchall()}


def get_target_ids(raw_db_path):
    """Get valid artwork IDs from the raw crawl database."""
    conn = sqlite3.connect(raw_db_path)
    cursor = conn.execute(
        "SELECT met_object_id FROM raw_artworks "
        "WHERE is_public_domain = 1 AND has_image = 1 AND data IS NOT NULL"
    )
    ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return ids


def parse_description(html):
    """Extract curatorial description from Met collection page HTML.

    The description lives inside a div with data-testid="read-more-content".
    Returns the plain text description, or None if not found / empty.
    """
    # Find the read-more-content element
    marker = 'data-testid="read-more-content"'
    idx = html.find(marker)
    if idx < 0:
        return None

    # Find the content after the marker — it's wrapped in nested divs:
    # <div ... data-testid="read-more-content"><div><div>CONTENT</div></div></div>
    # We need to find the inner content between the first > after marker and the
    # closing </div></div></div> sequence that ends the section.
    start = html.find(">", idx + len(marker))
    if start < 0:
        return None
    start += 1  # skip past the >

    # The section ends before </section> or the aside element
    end = html.find("</section>", start)
    if end < 0:
        end = len(html)

    chunk = html[start:end]

    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", chunk)
    # Decode HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&#x27;", "'").replace("&quot;", '"')
    text = text.replace("&#39;", "'").replace("&nbsp;", " ")
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Filter out empty or trivially short results
    if len(text) < 20:
        return None

    return text


async def fetch_page(session, object_id, proxy=None):
    """Fetch a single Met collection page. Returns (object_id, description|None, status)."""
    url = f"{MET_BASE}/{object_id}"
    try:
        async with session.get(url, proxy=proxy, headers=HEADERS) as resp:
            if resp.status == 200:
                html = await resp.text()
                desc = parse_description(html)
                return object_id, desc, "ok"
            elif resp.status == 404:
                return object_id, None, "404"
            elif resp.status in (429, 403):
                return object_id, None, "throttled"
            else:
                return object_id, None, f"http:{resp.status}"
    except Exception as e:
        return object_id, None, f"exc:{type(e).__name__}"


async def crawl(raw_db_path=RAW_DB_PATH, pages_db_path=PAGES_DB_PATH, limit=None, proxy_url=None):
    conn = init_db(pages_db_path)
    existing_ids = get_existing_ids(conn)
    log(f"Already fetched: {len(existing_ids)}")

    target_ids = get_target_ids(raw_db_path)
    log(f"Target artworks: {len(target_ids)}")

    all_missing = [oid for oid in target_ids if oid not in existing_ids]
    missing_ids = all_missing[:limit] if limit else all_missing
    log(f"Missing: {len(all_missing)} | To fetch this run: {len(missing_ids)}")

    if not missing_ids:
        log("Nothing to fetch.")
        conn.close()
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {"ok": 0, "with_desc": 0, "no_desc": 0, "not_found": 0, "throttled": 0, "errors": 0}
    pending_inserts = []
    lock = asyncio.Lock()
    error_samples = []
    completed = 0
    total = len(missing_ids)
    start_time = time.time()
    last_log = start_time

    async def fetch_with_sem(oid):
        nonlocal completed, last_log
        async with sem:
            for attempt in range(MAX_RETRIES + 1):
                result = await fetch_page(session, oid, proxy=proxy_url)
                object_id, desc, status = result
                if status != "throttled" or attempt == MAX_RETRIES:
                    break
                stats["throttled"] += 1
                await asyncio.sleep(2 ** attempt)

            async with lock:
                if status == "ok":
                    stats["ok"] += 1
                    if desc:
                        stats["with_desc"] += 1
                    else:
                        stats["no_desc"] += 1
                    pending_inserts.append((object_id, desc))
                    completed += 1
                elif status == "404":
                    stats["not_found"] += 1
                    pending_inserts.append((object_id, None))
                    completed += 1
                elif status == "throttled":
                    stats["errors"] += 1
                    completed += 1
                else:
                    stats["errors"] += 1
                    if len(error_samples) < 10:
                        error_samples.append(f"{object_id}:{status}")
                    pending_inserts.append((object_id, None))
                    completed += 1

                if len(pending_inserts) >= COMMIT_EVERY:
                    conn.executemany(
                        "INSERT OR IGNORE INTO page_content (met_object_id, description) VALUES (?, ?)",
                        pending_inserts,
                    )
                    conn.commit()
                    pending_inserts.clear()

                now = time.time()
                if now - last_log >= 5:
                    last_log = now
                    elapsed = now - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    eta_m = (total - completed) / rate / 60 if rate > 0 else 0
                    log(
                        f"  [{completed}/{total}] "
                        f"{rate:.1f} req/s | "
                        f"ETA {eta_m:.1f}m | "
                        f"desc={stats['with_desc']} no_desc={stats['no_desc']} "
                        f"404={stats['not_found']} err={stats['errors']}"
                    )

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*[fetch_with_sem(oid) for oid in missing_ids])

    # Final commit
    if pending_inserts:
        conn.executemany(
            "INSERT OR IGNORE INTO page_content (met_object_id, description) VALUES (?, ?)",
            pending_inserts,
        )
        conn.commit()

    elapsed = time.time() - start_time
    log(f"\nDone in {elapsed/60:.1f} minutes ({elapsed/3600:.1f} hours)")
    log(f"  Fetched: {completed}")
    log(f"  With description: {stats['with_desc']}")
    log(f"  Without description: {stats['no_desc']}")
    log(f"  Not found (404): {stats['not_found']}")
    log(f"  Throttled (retried): {stats['throttled']}")
    log(f"  Errors: {stats['errors']}")
    if error_samples:
        log(f"  Sample errors: {error_samples}")

    total_in_db = conn.execute("SELECT COUNT(*) FROM page_content").fetchone()[0]
    with_desc = conn.execute(
        "SELECT COUNT(*) FROM page_content WHERE description IS NOT NULL"
    ).fetchone()[0]
    log(f"\nDB totals: {total_in_db} rows, {with_desc} with descriptions")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Crawl Met Museum collection pages for descriptions")
    parser.add_argument("--limit", type=int, help="Max pages to fetch (for testing)")
    parser.add_argument("--raw-db", default=RAW_DB_PATH, help="Path to raw_artworks.db")
    parser.add_argument("--db", default=PAGES_DB_PATH, help="Path to output met_pages.db")
    parser.add_argument("--proxy", type=str, help="Proxy URL (optional, not usually needed)")
    args = parser.parse_args()
    asyncio.run(crawl(raw_db_path=args.raw_db, pages_db_path=args.db, limit=args.limit, proxy_url=args.proxy))


if __name__ == "__main__":
    main()
