#!/usr/bin/env python3
"""Generate captions for artworks using Claude Haiku via the Batch API.

Usage:
    python scripts/caption.py submit [--limit N] [--batch-size N] [--force] [--confirm]
    python scripts/caption.py poll
    python scripts/caption.py collect
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import anthropic

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DB = os.path.join(SCRIPT_DIR, "../data/raw_artworks.db")
BATCHES_DB = os.path.join(SCRIPT_DIR, "../data/batches.db")
CAPTIONS_DB = os.path.join(SCRIPT_DIR, "../data/captions.db")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 384
API_BATCH_LIMIT = 100_000

# Cost estimates for Haiku 4.5 (batch pricing: 50% of standard)
# Standard: $0.80/MTok input, $4/MTok output → Batch: $0.40/MTok input, $2/MTok output
# ~1400 input tokens/request (image ~1000 + system ~250 + metadata ~150), ~100 output tokens
INPUT_COST_PER_MTOK = 0.40
OUTPUT_COST_PER_MTOK = 2.00
EST_INPUT_TOKENS = 1400
EST_OUTPUT_TOKENS = 100

SYSTEM_PROMPT = """\
You are an art cataloger for the Metropolitan Museum of Art. You write concise visual \
descriptions of artworks for a searchable database.

Rules:
- Describe what the ARTWORK depicts or looks like, NOT the photograph of it. The image \
you see is a catalog photograph. Do not describe it as "a photograph" or mention the \
photographic background, unless the artwork itself IS a photograph.
- Many catalog images are black-and-white archival photographs. If the image appears \
grayscale, do NOT describe the artwork as black, gray, or monochrome — instead, rely on \
the metadata for material and color information. Describe the artwork's likely original \
appearance, not the photograph's tonal range.
- Ground your description in the provided metadata. Use the correct medium, materials, \
and cultural origin. Do not guess materials.
- Focus on: visual content, subject matter, composition, colors/tones, and notable \
stylistic features.
- Write 2-3 sentences. Be specific and factual."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {
            "type": "string",
            "description": "2-3 sentence visual description of the artwork",
        },
    },
    "required": ["caption"],
    "additionalProperties": False,
}

# Metadata fields to extract from raw JSON and include in prompt
METADATA_FIELDS = [
    ("Title", "title"),
    ("Object type", "objectName"),
    ("Medium", "medium"),
    ("Department", "department"),
    ("Classification", "classification"),
    ("Culture", "culture"),
    ("Date", "objectDate"),
]


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_batches_db():
    """Create batches.db schema if needed."""
    conn = sqlite3.connect(BATCHES_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            ended_at DATETIME,
            collected_at DATETIME,
            succeeded INTEGER DEFAULT 0,
            errored INTEGER DEFAULT 0,
            expired INTEGER DEFAULT 0,
            canceled INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS batch_items (
            met_object_id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL
        );
    """)
    conn.close()
    return sqlite3.connect(BATCHES_DB)


def init_captions_db():
    """Create captions.db schema if needed."""
    conn = sqlite3.connect(CAPTIONS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS captions (
            met_object_id INTEGER PRIMARY KEY,
            caption TEXT
        )
    """)
    conn.commit()
    conn.close()
    return sqlite3.connect(CAPTIONS_DB)


def get_known_ids(batches_conn, captions_conn):
    """Return set of met_object_ids already captioned or still in-flight."""
    known = set()

    # Already captioned
    for row in captions_conn.execute("SELECT met_object_id FROM captions"):
        known.add(row[0])

    # Still in-flight (not yet ended/collected/failed)
    for row in batches_conn.execute(
        "SELECT met_object_id FROM batch_items bi "
        "JOIN batches b ON bi.batch_id = b.batch_id "
        "WHERE b.status IN ('submitted', 'in_progress')"
    ):
        known.add(row[0])

    return known


def extract_metadata(data):
    """Extract metadata dict from raw Met API JSON."""
    meta = {}
    for _label, key in METADATA_FIELDS:
        val = data.get(key)
        if val and str(val).strip():
            meta[key] = str(val).strip()
    return meta


def build_metadata_block(metadata):
    """Build metadata text block for the prompt, only including non-empty fields."""
    lines = []
    for label, key in METADATA_FIELDS:
        val = metadata.get(key)
        if val:
            lines.append(f"- {label}: {val}")
    return "Artwork metadata:\n" + "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

def make_request(met_object_id, image_url, metadata):
    """Build a single Batch API request dict."""
    metadata_block = build_metadata_block(metadata)
    user_text = f"{metadata_block}\n\nDescribe this artwork." if metadata_block else "Describe this artwork."

    return {
        "custom_id": str(met_object_id),
        "params": {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": image_url},
                        },
                        {
                            "type": "text",
                            "text": user_text,
                        },
                    ],
                }
            ],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": OUTPUT_SCHEMA,
                }
            },
        },
    }


def cmd_submit(args):
    batches_conn = init_batches_db()
    captions_conn = init_captions_db()

    if args.force:
        known_ids = set()
        log("Force mode: re-captioning all artworks")
    else:
        known_ids = get_known_ids(batches_conn, captions_conn)
        log(f"Already known IDs: {len(known_ids)} (captioned + submitted)")

    # Load candidates from raw DB
    raw_conn = sqlite3.connect(RAW_DB)
    cursor = raw_conn.execute(
        "SELECT met_object_id, data FROM raw_artworks "
        "WHERE is_public_domain=1 AND has_image=1 AND data IS NOT NULL"
    )

    candidates = []
    for met_id, data_json in cursor:
        if met_id in known_ids:
            continue
        data = json.loads(data_json)
        image_url = data.get("primaryImageSmall", "")
        if not image_url:
            continue
        metadata = extract_metadata(data)
        candidates.append((met_id, image_url, metadata))
        if args.limit and len(candidates) >= args.limit:
            break

    raw_conn.close()

    if not candidates:
        log("No new artworks to caption.")
        batches_conn.close()
        captions_conn.close()
        return

    # Cost estimate
    total = len(candidates)
    est_input_cost = (total * EST_INPUT_TOKENS / 1_000_000) * INPUT_COST_PER_MTOK
    est_output_cost = (total * EST_OUTPUT_TOKENS / 1_000_000) * OUTPUT_COST_PER_MTOK
    est_total = est_input_cost + est_output_cost
    num_batches = (total + args.batch_size - 1) // args.batch_size

    log(f"\nSubmission plan:")
    log(f"  Artworks to caption: {total:,}")
    log(f"  Batches: {num_batches} (batch size: {args.batch_size:,})")
    log(f"  Model: {MODEL}")
    log(f"  Estimated cost: ${est_total:.2f} "
        f"(input: ${est_input_cost:.2f}, output: ${est_output_cost:.2f})")

    if not args.confirm:
        response = input("\nProceed? [y/N] ").strip().lower()
        if response != "y":
            log("Aborted.")
            batches_conn.close()
            captions_conn.close()
            return

    # Submit batches
    client = anthropic.Anthropic()

    for batch_idx in range(num_batches):
        start = batch_idx * args.batch_size
        end = min(start + args.batch_size, total)
        chunk = candidates[start:end]

        requests = [make_request(mid, url, meta) for mid, url, meta in chunk]

        log(f"\nSubmitting batch {batch_idx + 1}/{num_batches} "
            f"({len(chunk):,} requests)...")

        result = client.messages.batches.create(requests=requests)

        # Record in batches.db
        batches_conn.execute(
            "INSERT INTO batches (batch_id, status, request_count) VALUES (?, ?, ?)",
            (result.id, result.processing_status, len(chunk)),
        )
        batches_conn.executemany(
            "INSERT OR IGNORE INTO batch_items (met_object_id, batch_id) VALUES (?, ?)",
            [(mid, result.id) for mid, _, _ in chunk],
        )
        batches_conn.commit()

        log(f"  Batch ID: {result.id}")
        log(f"  Status: {result.processing_status}")

    batches_conn.close()
    captions_conn.close()
    log("\nAll batches submitted.")


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------

def cmd_poll(_args):
    if not os.path.exists(BATCHES_DB):
        log("No batches.db found. Run 'submit' first.")
        return

    batches_conn = sqlite3.connect(BATCHES_DB)
    rows = batches_conn.execute(
        "SELECT batch_id, status, request_count, created_at FROM batches "
        "WHERE status NOT IN ('collected', 'failed') "
        "ORDER BY created_at"
    ).fetchall()

    if not rows:
        log("No active batches to poll.")
        batches_conn.close()
        return

    client = anthropic.Anthropic()

    log(f"Polling {len(rows)} batch(es)...\n")
    log(f"{'Batch ID':<30} {'Status':<15} {'Requests':>10} "
        f"{'Succeeded':>10} {'Errored':>8} {'Expired':>8} {'Canceled':>9}")
    log("-" * 100)

    for batch_id, old_status, request_count, created_at in rows:
        result = client.messages.batches.retrieve(batch_id)

        counts = result.request_counts
        new_status = result.processing_status

        batches_conn.execute(
            "UPDATE batches SET status=?, succeeded=?, errored=?, expired=?, canceled=?, "
            "ended_at=? WHERE batch_id=?",
            (
                new_status,
                counts.succeeded,
                counts.errored,
                counts.expired,
                counts.canceled,
                result.ended_at.isoformat() if result.ended_at else None,
                batch_id,
            ),
        )

        log(f"{batch_id:<30} {new_status:<15} {request_count:>10,} "
            f"{counts.succeeded:>10,} {counts.errored:>8,} "
            f"{counts.expired:>8,} {counts.canceled:>9,}")

    batches_conn.commit()
    batches_conn.close()
    log("\nDone.")


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------

def cmd_collect(_args):
    if not os.path.exists(BATCHES_DB):
        log("No batches.db found. Run 'submit' first.")
        return

    batches_conn = sqlite3.connect(BATCHES_DB)
    captions_conn = init_captions_db()

    rows = batches_conn.execute(
        "SELECT batch_id, request_count FROM batches WHERE status = 'ended' "
        "ORDER BY created_at"
    ).fetchall()

    if not rows:
        log("No ended batches to collect. Run 'poll' to check status.")
        batches_conn.close()
        captions_conn.close()
        return

    client = anthropic.Anthropic()

    for batch_id, request_count in rows:
        log(f"\nCollecting batch {batch_id} ({request_count:,} requests)...")

        succeeded = 0
        errored = 0
        malformed = 0
        batch = []

        for item in client.messages.batches.results(batch_id):
            met_object_id = int(item.custom_id)

            if item.result.type != "succeeded":
                errored += 1
                if errored <= 5:
                    log(f"  Error for {met_object_id}: {item.result.type}")
                continue

            # Parse structured JSON output
            message = item.result.message
            text = "\n".join(
                block.text for block in message.content if block.type == "text"
            )

            try:
                data = json.loads(text)
                caption = data["caption"]
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                malformed += 1
                if malformed <= 5:
                    log(f"  Malformed response for {met_object_id}: {e}")
                continue

            batch.append((met_object_id, caption))
            succeeded += 1

            if len(batch) >= 5000:
                captions_conn.executemany(
                    "INSERT OR REPLACE INTO captions (met_object_id, caption) "
                    "VALUES (?, ?)",
                    batch,
                )
                captions_conn.commit()
                batch = []

        # Final batch
        if batch:
            captions_conn.executemany(
                "INSERT OR REPLACE INTO captions (met_object_id, caption) "
                "VALUES (?, ?)",
                batch,
            )
            captions_conn.commit()

        # Mark as collected
        batches_conn.execute(
            "UPDATE batches SET status='collected', collected_at=? WHERE batch_id=?",
            (datetime.now(timezone.utc).isoformat(), batch_id),
        )
        batches_conn.commit()

        log(f"  Collected: {succeeded:,} succeeded, {errored:,} errored, "
            f"{malformed:,} malformed")

    total = captions_conn.execute("SELECT COUNT(*) FROM captions").fetchone()[0]
    log(f"\nTotal captions in DB: {total:,}")

    batches_conn.close()
    captions_conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate artwork captions via Claude Batch API"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # submit
    p_submit = sub.add_parser("submit", help="Submit captioning batches")
    p_submit.add_argument("--limit", type=int, default=0,
                          help="Max artworks to submit (0 = all)")
    p_submit.add_argument("--batch-size", type=int, default=API_BATCH_LIMIT,
                          help=f"Requests per batch (max {API_BATCH_LIMIT:,})")
    p_submit.add_argument("--force", action="store_true",
                          help="Re-caption all artworks (ignore existing)")
    p_submit.add_argument("--confirm", action="store_true",
                          help="Skip interactive confirmation")

    # poll
    sub.add_parser("poll", help="Poll batch status")

    # collect
    sub.add_parser("collect", help="Collect results from ended batches")

    args = parser.parse_args()

    if args.command == "submit":
        if args.batch_size > API_BATCH_LIMIT:
            log(f"Error: batch-size cannot exceed {API_BATCH_LIMIT:,}")
            sys.exit(1)
        cmd_submit(args)
    elif args.command == "poll":
        cmd_poll(args)
    elif args.command == "collect":
        cmd_collect(args)


if __name__ == "__main__":
    main()
