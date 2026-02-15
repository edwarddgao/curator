#!/usr/bin/env python3
"""Transform raw Met API JSON into structured artworks.db with FTS5 index."""

import sqlite3
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DB = os.path.join(SCRIPT_DIR, "../data/raw_artworks.db")
OUT_DB = os.path.join(SCRIPT_DIR, "../data/artworks.db")
CAPTIONS_DB = os.path.join(SCRIPT_DIR, "../data/captions.db")
PAGES_DB = os.path.join(SCRIPT_DIR, "../data/met_pages.db")
BATCH_SIZE = 5000


def log(msg):
    print(msg, flush=True)


def safe_int(val):
    """Convert string to int, returning None for empty/invalid values."""
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def nullable(val):
    """Convert empty string to None."""
    if val is None or (isinstance(val, str) and val.strip() == ""):
        return None
    return val


def create_schema(conn):
    conn.executescript("""
        CREATE TABLE artworks (
            id INTEGER PRIMARY KEY,
            met_object_id INTEGER UNIQUE,
            title TEXT NOT NULL,
            artist_name TEXT,
            artist_bio TEXT,
            artist_nationality TEXT,
            artist_birth_year INTEGER,
            artist_death_year INTEGER,
            object_date TEXT,
            date_begin INTEGER,
            date_end INTEGER,
            medium TEXT,
            dimensions TEXT,
            department TEXT,
            classification TEXT,
            culture TEXT,
            period TEXT,
            caption TEXT,
            description TEXT,
            image_url TEXT,
            thumbnail_url TEXT NOT NULL,
            object_url TEXT,
            credit_line TEXT,
            accession_number TEXT,
            gallery_number TEXT,
            is_highlight BOOLEAN DEFAULT 0
        );

        CREATE INDEX idx_artworks_artist ON artworks(artist_name);
        CREATE INDEX idx_artworks_department ON artworks(department);
        CREATE INDEX idx_artworks_date ON artworks(date_begin, date_end);
        CREATE INDEX idx_artworks_classification ON artworks(classification);
    """)


def create_fts(conn):
    conn.executescript("""
        CREATE VIRTUAL TABLE artworks_fts USING fts5(
            title,
            artist_name,
            medium,
            caption,
            description,
            culture,
            period,
            department,
            content=artworks,
            content_rowid=id
        );

        INSERT INTO artworks_fts(artworks_fts) VALUES('rebuild');
    """)


def load_captions(captions_db_path):
    """Load captions from captions.db if it exists."""
    if not os.path.exists(captions_db_path):
        return {}
    log(f"Loading captions from {captions_db_path}...")
    conn = sqlite3.connect(captions_db_path)
    cursor = conn.execute("SELECT met_object_id, caption FROM captions")
    captions = {}
    for row in cursor:
        captions[row[0]] = row[1]
    conn.close()
    log(f"  Loaded {len(captions)} captions")
    return captions


def load_page_content(pages_db_path):
    """Load page descriptions from met_pages.db if it exists."""
    if not os.path.exists(pages_db_path):
        return {}
    log(f"Loading page content from {pages_db_path}...")
    conn = sqlite3.connect(pages_db_path)
    cursor = conn.execute("SELECT met_object_id, description FROM page_content WHERE description IS NOT NULL")
    pages = {}
    for row in cursor:
        pages[row[0]] = row[1]
    conn.close()
    log(f"  Loaded {len(pages)} descriptions")
    return pages


def parse_artwork(data, captions, pages):
    """Parse Met API JSON into a tuple for insertion."""
    met_id = data["objectID"]

    title = data.get("title") or data.get("objectName") or "Untitled"
    thumbnail_url = data.get("primaryImageSmall", "")
    if not thumbnail_url:
        return None  # skip if no thumbnail

    caption = captions.get(met_id)
    description = pages.get(met_id)

    return (
        met_id,
        title,
        nullable(data.get("artistDisplayName")),
        nullable(data.get("artistDisplayBio")),
        nullable(data.get("artistNationality")),
        safe_int(data.get("artistBeginDate")),
        safe_int(data.get("artistEndDate")),
        nullable(data.get("objectDate")),
        data.get("objectBeginDate"),  # already int in API
        data.get("objectEndDate"),    # already int in API
        nullable(data.get("medium")),
        nullable(data.get("dimensions")),
        nullable(data.get("department")),
        nullable(data.get("classification")),
        nullable(data.get("culture")),
        nullable(data.get("period")),
        caption,
        description,
        nullable(data.get("primaryImage")),
        thumbnail_url,
        nullable(data.get("objectURL")),
        nullable(data.get("creditLine")),
        nullable(data.get("accessionNumber")),
        nullable(data.get("GalleryNumber")),
        1 if data.get("isHighlight") else 0,
    )


INSERT_SQL = """
    INSERT INTO artworks (
        met_object_id, title, artist_name, artist_bio, artist_nationality,
        artist_birth_year, artist_death_year, object_date, date_begin, date_end,
        medium, dimensions, department, classification, culture, period,
        caption, description, image_url, thumbnail_url, object_url,
        credit_line, accession_number, gallery_number, is_highlight
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def build(raw_db=RAW_DB, out_db=OUT_DB, captions_db=CAPTIONS_DB, pages_db=PAGES_DB):
    if not os.path.exists(raw_db):
        log(f"Error: {raw_db} not found")
        sys.exit(1)

    # Delete existing output DB
    if os.path.exists(out_db):
        os.remove(out_db)
        log(f"Removed existing {out_db}")

    # Load captions and page content if available
    captions = load_captions(captions_db)
    pages = load_page_content(pages_db)

    # Create output DB
    out_conn = sqlite3.connect(out_db)
    out_conn.execute("PRAGMA journal_mode=WAL")
    out_conn.execute("PRAGMA synchronous=NORMAL")
    create_schema(out_conn)

    # Read raw DB
    raw_conn = sqlite3.connect(raw_db)
    cursor = raw_conn.execute(
        "SELECT met_object_id, data FROM raw_artworks "
        "WHERE is_public_domain=1 AND has_image=1 AND data IS NOT NULL"
    )

    inserted = 0
    skipped = 0
    errors = 0
    batch = []

    log("Processing artworks...")
    for met_id, data_json in cursor:
        try:
            data = json.loads(data_json)
            row = parse_artwork(data, captions, pages)
            if row is None:
                skipped += 1
                continue
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                out_conn.executemany(INSERT_SQL, batch)
                out_conn.commit()
                inserted += len(batch)
                batch = []
                log(f"  Inserted {inserted}...")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            errors += 1
            if errors <= 5:
                log(f"  Error on {met_id}: {e}")

    # Final batch
    if batch:
        out_conn.executemany(INSERT_SQL, batch)
        out_conn.commit()
        inserted += len(batch)

    raw_conn.close()
    log(f"Inserted {inserted} artworks (skipped {skipped}, errors {errors})")

    # Build FTS5 index
    log("Building FTS5 index...")
    create_fts(out_conn)
    out_conn.commit()

    # VACUUM
    log("Running VACUUM...")
    out_conn.execute("VACUUM")
    out_conn.close()

    # Verify
    verify_conn = sqlite3.connect(out_db)
    count = verify_conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
    fts_count = verify_conn.execute(
        "SELECT COUNT(*) FROM artworks_fts WHERE artworks_fts MATCH 'painting'"
    ).fetchone()[0]
    size_mb = os.path.getsize(out_db) / (1024 * 1024)
    verify_conn.close()

    log(f"\nDone: {out_db}")
    log(f"  Rows: {count}")
    log(f"  FTS test ('painting'): {fts_count} matches")
    log(f"  Size: {size_mb:.1f} MB")


if __name__ == "__main__":
    build()
