# Art Curator

An MCP server that gives Claude direct SQL access to 243,000+ captioned artworks from the Metropolitan Museum of Art. Claude acts as an art curator — searching, recommending, and displaying artwork images inline in conversation.

## How it works

Users ask Claude about art in natural language. Claude writes SQL queries against a structured database with full-text search, curates the best matches, and presents them with commentary and inline images.

```
User: "Show me paintings of boats in storms"
Claude: *queries FTS for storm+ship, selects best matches, shows images with art historical context*
```

## Tools

| Tool | Description |
|---|---|
| `query_artworks` | Execute read-only SQL against the artworks database (243K rows, FTS5 index) |
| `show_artwork` | Display a single artwork image inline by ID |

## Database schema

```sql
artworks (
  id, met_object_id, title, artist_name, artist_bio, artist_nationality,
  artist_birth_year, artist_death_year, object_date, date_begin, date_end,
  medium, dimensions, department, classification, culture, period,
  caption,        -- AI-generated visual description
  description,    -- curatorial essay from Met website (~41% coverage)
  image_url, thumbnail_url, object_url,
  credit_line, accession_number, gallery_number, is_highlight
)

artworks_fts (FTS5 over: title, artist_name, medium, caption, description, culture, period, department)
```

## Setup

### Use the deployed server

Add to your Claude Code project (`.mcp.json`):

```json
{
  "mcpServers": {
    "art-curator": {
      "type": "http",
      "url": "https://art-curator.fly.dev/mcp"
    }
  }
}
```

### Run locally

Requires `data/artworks.db` (built from the data pipeline).

```bash
npm install
npm run build   # builds viewer UI
npm start       # starts server on port 3001
```

### Deploy

```bash
fly deploy --remote-only
```

## Data pipeline

The database is built from three sources:

1. **Met API crawl** (`scripts/crawl.py`) — 243K public domain artworks with images
2. **AI captioning** (`scripts/caption.py`) — visual descriptions via Claude Haiku Batch API
3. **Met website crawl** (`scripts/crawl_pages.py`) — curatorial descriptions (~99K)
4. **Build** (`scripts/build_db.py`) — merges all sources into `artworks.db` with FTS5

## Stack

- Node.js + Express + MCP SDK (Streamable HTTP, stateless)
- SQLite + FTS5 (better-sqlite3, read-only)
- Vite + vite-plugin-singlefile (viewer UI)
- Fly.io (auto-stop/auto-start, scale-to-zero)
