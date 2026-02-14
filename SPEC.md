# Art Curator MCP App — Specification

> An MCP App that gives Claude direct SQL access to a database of captioned museum artworks, enabling Claude to act as an art curator — searching, recommending, and discussing art with artwork images embedded inline in the conversation.

## References

- https://modelcontextprotocol.io/docs/extensions/apps
- https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp
- https://support.claude.com/en/articles/11176164-pre-built-web-connectors-using-remote-mcp
- https://support.claude.com/en/articles/11724452-using-the-connectors-directory-to-extend-claude-s-capabilities

---

## 1. Product Vision

Claude acts as an **art curator**. Users ask Claude about art in natural language ("find me paintings of boats in storms", "show me Vermeer", "what impressionist landscapes do you have?"). Claude queries a database of captioned museum artworks, curates the best matches, and displays them as inline images embedded directly in the chat.

**This is NOT visual similarity search.** The original artalike app's "find similar" feature (FAISS embeddings) is dropped entirely. Instead, Claude's intelligence is the search engine — it writes SQL queries, reads results, selects the best matches, and presents them with commentary.

**Key differentiator from a standalone web app:** The art lives inside the conversation. Claude can discuss an artwork's history, compare pieces, explain techniques, and follow up on the user's interests — all with the visual context right there in the chat.

---

## 2. Architecture

### 2.1 Runtime — Monolith

Single Node.js process. No external services.

```
[Claude] ──HTTPS──▶ [Node.js + Express]
                       ├─ MCP protocol handler (StreamableHTTPServerTransport)
                       ├─ SQLite + FTS5 (embedded, read-only)
                       └─ Bundled HTML (artwork image viewer resource)
```

- **Server:** Node.js + Express + `@modelcontextprotocol/sdk`
- **Database:** SQLite via `better-sqlite3`, opened in read-only mode
- **UI:** Single HTML file bundled with Vite + `vite-plugin-singlefile`
- **Deploy:** Single container on Fly.io (or equivalent)

### 2.2 Data Pipeline — Offline, Python

Separate from the runtime server. Produces a `.db` file that ships inside the container.

```
crawl.py ──▶ caption.py ──▶ build_db.py ──▶ artworks.db
   │              │               │
   │              │               └─ Build SQLite DB + FTS5 index
   │              └─ Caption images via Haiku 4.5 Batch API
   └─ Crawl Met Museum API, store raw JSON
```

Each step is independent and re-runnable. Outputs of each step serve as inputs to the next. No step depends on the runtime server.

### 2.3 Data Update Flow

```
Re-run pipeline ──▶ New artworks.db ──▶ Redeploy container
```

Data updates require rebuilding the DB and redeploying. This is acceptable for V1 since museum collections change infrequently.

---

## 3. Data Source

### V1: The Metropolitan Museum of Art only

- **API:** `https://collectionapi.metmuseum.org/public/collection/v1/`
- **Auth:** None required
- **Rate limit:** Documented as 80 req/s, but actual limit is ~80 requests per rolling 30-60s window per IP, enforced by Imperva WAF. Bypassed via residential proxy rotation (see §9 for details).
- **Bulk metadata:** CSV dump on GitHub ([metmuseum/openaccess](https://github.com/metmuseum/openaccess)) — 484,956 objects, all metadata fields, no image URLs. Used to identify public domain IDs without wasting API calls.
- **Scope:** Public domain works only (`isPublicDomain: true`). Non-public-domain works have empty image URLs and are excluded entirely.
- **Estimated size:** ~248K public domain works (from CSV filtering, more accurate than the ~260K estimate from search API)
- **Language:** English

### Why Met only

- Zero auth friction (no API key needed)
- Large public domain collection with direct image URLs
- English metadata eliminates translation concerns
- Existing crawler code in artalike can be adapted

### Future expansion (not V1)

Other English-language museums with excellent open APIs:
- **Art Institute of Chicago** — 117K artworks, 50K+ CC0 images, Elasticsearch search, IIIF images
- **Cleveland Museum of Art** — 64K artworks, 37K+ images in 3 resolutions, excellent API
- **Harvard Art Museums** — 224K objects (requires free API key)

The Louvre was dropped from V1 due to French-only metadata.

---

## 4. MCP Tools

### 4.1 `query_artworks` — SQL execution (no UI)

Claude writes raw SQL queries against the artworks database. Returns results as JSON text. Registered via `server.tool()` (not `registerAppTool`) since it has no UI.

**Input:** `{ sql: string }` — read-only SQL query (SELECT only)
**Returns:** Text content with JSON array of matching rows. Max 100 rows. No UI.

### 4.2 `show_artwork` — Display single artwork image (with UI)

Displays a single artwork image inline in the chat. Claude calls this after curating search results. To show multiple artworks, Claude calls this tool multiple times. Artist name, date, and commentary are written by Claude as conversation text — the UI is just the image.

**Input:** `{ id: number }` — single artwork ID
**Returns:** Text content with full artwork metadata (JSON) + `structuredContent.artwork` for the UI.

```typescript
const resourceUri = "ui://art-curator/carousel.html";

registerAppTool(server, "show_artwork", {
  title: "Show Artwork",
  description: "Display a single artwork image inline. Call query_artworks first to find artworks, then pass one ID here. Call multiple times to show multiple images.",
  inputSchema: z.object({
    id: z.number().int().describe("Artwork ID to display"),
  }),
  _meta: { ui: { resourceUri } }
});
```

### 4.3 Interaction Flow

```
1. User: "Find me paintings of boats in storms"
2. Claude calls query_artworks({ sql: "SELECT * FROM artworks_fts WHERE artworks_fts MATCH 'boats storms' LIMIT 20" })
   → Returns 20 rows of metadata as JSON text to Claude
3. Claude reads results, picks the best matches
4. Claude calls show_artwork({ id: 4523 }) for each selected artwork
   → Each call renders the artwork image inline in the chat
   → Claude also receives full metadata as text
5. Claude writes commentary: artist name, date, description, context
```

Claude can iterate: if the first query returns poor results, it can try different keywords, add filters, or broaden the search.

---

## 5. Database Schema

### 5.1 Main table — flat, denormalized

```sql
CREATE TABLE artworks (
  id INTEGER PRIMARY KEY,
  met_object_id INTEGER UNIQUE,
  title TEXT NOT NULL,
  artist_name TEXT,
  artist_bio TEXT,              -- e.g. "Dutch, Delft 1632–1675 Delft"
  artist_nationality TEXT,
  artist_birth_year INTEGER,
  artist_death_year INTEGER,
  object_date TEXT,             -- display string from API
  date_begin INTEGER,
  date_end INTEGER,
  medium TEXT,
  dimensions TEXT,
  department TEXT,
  classification TEXT,
  culture TEXT,
  period TEXT,
  caption TEXT,                 -- Haiku-generated visual description
  keywords TEXT,                -- comma-separated searchable terms
  image_url TEXT,               -- primaryImage (full resolution, may be empty for some works)
  thumbnail_url TEXT NOT NULL,  -- primaryImageSmall (web-size ~480-800px, fallback for display + captioning)
  object_url TEXT,              -- Met museum page URL
  credit_line TEXT,
  accession_number TEXT,
  gallery_number TEXT,
  is_highlight BOOLEAN DEFAULT 0
);

CREATE INDEX idx_artworks_artist ON artworks(artist_name);
CREATE INDEX idx_artworks_department ON artworks(department);
CREATE INDEX idx_artworks_date ON artworks(date_begin, date_end);
CREATE INDEX idx_artworks_classification ON artworks(classification);
```

### 5.2 FTS5 virtual table

```sql
CREATE VIRTUAL TABLE artworks_fts USING fts5(
  title,
  artist_name,
  medium,
  caption,
  keywords,
  culture,
  period,
  department,
  content=artworks,
  content_rowid=id
);
```

### 5.3 Design decisions

- **Flat/denormalized:** Fastest for single-table queries. No JOINs needed. Claude writes simpler SQL.
- **FTS5:** Inverted index for fast text search with BM25 ranking. Claude can use `MATCH` for keyword search or `LIKE` for exact patterns. Both available.
- **No tags table:** Met has structured AAT/Wikidata tags, but they're inconsistent across museums and add schema complexity. The `keywords` field (from captioning) serves the same purpose.

---

## 6. Captioning Pipeline

### 6.1 Model and cost

- **Model:** Claude Haiku 4.5 via Batch API
- **Input:** ~350 tokens per image (512px), ~50 tokens for prompt
- **Output:** ~150 tokens per caption (description + keywords)
- **Cost:** ~$0.00045/image → ~$112 for all ~248K public domain Met works (well within $500 budget)

### 6.2 Captioning prompt

```
Describe this artwork image in 2-3 sentences focusing on what is visually depicted: subjects, colors, composition, lighting, and scene. Then list 10-15 searchable keywords covering the visual content, mood, style, and any identifiable objects or themes.

Format:
Caption: [2-3 sentence visual description]
Keywords: [comma-separated keywords]
```

Example output:
```
Caption: A woman in a blue dress stands beside a sunlit window, reaching for a silver water pitcher on a wooden table. The scene is bathed in soft, warm light from the left, with a stained glass window and a large wall map visible in the background. The composition is intimate and contemplative, with careful attention to the play of light on fabric and metal.
Keywords: woman, blue dress, window, sunlight, pitcher, silver, table, interior, domestic, contemplative, warm light, Baroque, portrait, still life, map
```

### 6.3 Pipeline step: `caption.py`

- Reads crawled artwork data from previous step
- For each public domain artwork with an image URL:
  - Sends the image URL to Claude Haiku 4.5 via Batch API
  - Parses the response into `caption` and `keywords` fields
- Stores results alongside artwork metadata
- Idempotent: skips already-captioned images on re-run

---

## 7. Security

### 7.1 SQL injection mitigation

Claude writes raw SQL, but the database is **read-only**:

1. **Read-only mode:** SQLite opened with `SQLITE_OPEN_READONLY` flag via `better-sqlite3`. No INSERT, UPDATE, DELETE, DROP, or any write operation is physically possible.
2. **Query timeout:** 5-second timeout per query. Kills runaway queries (e.g., cartesian products).
3. **Result limit:** Server enforces a hard cap of 100 rows per query response, regardless of what Claude's SQL says.
4. **Statement validation:** Only `SELECT` statements are executed. Any query not starting with `SELECT` (after normalization) is rejected.
5. **Public data:** All data is public museum metadata and AI-generated captions. No sensitive information exists in the database. Information disclosure is a non-issue.

### 7.2 MCP App sandbox

- The artwork viewer runs in a sandboxed iframe controlled by the host (Claude)
- No access to parent window DOM, cookies, or local storage
- Communication only via postMessage (abstracted by `@modelcontextprotocol/ext-apps` `App` class)
- Image URLs point to Met Museum CDN — no self-hosted images
- CSP `resourceDomains` allowlists `https://images.metmuseum.org` for image loading

### 7.3 Authentication

- **None.** The MCP server is publicly accessible. No OAuth, no API keys.
- All data served is public domain (CC0) museum metadata
- Read-only access prevents abuse

---

## 8. MCP App UI (Artwork Viewer)

### 8.1 Technology

- Vanilla TypeScript, no framework
- Bundled into single HTML file via Vite + `vite-plugin-singlefile`
- Served as a `ui://` resource by the MCP server

### 8.2 Design

- **Layout:** Single `<img>` element, `width: 100%`, filling the iframe width
- **Content:** Just the artwork image — no text overlay, no cards, no controls
- **Artist name, date, commentary:** Written by Claude as conversation text, not in the UI
- **Multiple images:** Claude calls `show_artwork` once per image. Each call produces a separate inline image in the chat.

### 8.3 Data flow

The UI receives artwork data via `app.ontoolresult`, which provides `structuredContent.artwork`. The image `src` is set to `image_url` (full resolution) with fallback to `thumbnail_url`.

### 8.4 State management

Each `show_artwork` tool call renders one image in its own iframe. No accumulated state. Claude remembers prior searches in its conversation context.

---

## 9. Data Pipeline — Detailed Steps

### Step 1: `crawl.py` — Crawl Met Museum API

**Input:** None (fetches from CSV + API)
**Output:** SQLite DB (`raw_artworks.db`) with raw JSON per object

**Strategy: CSV filter + API fetch**

The Met API has no bulk metadata endpoint — each object must be fetched individually for image URLs. However, the Met publishes a CSV dump on GitHub with all metadata, which we use to identify exactly which objects to fetch.

1. **CSV download:** Download `MetObjects.csv` from GitHub ([metmuseum/openaccess](https://github.com/metmuseum/openaccess)). Filter for `Is Public Domain == True` to get ~248K object IDs. This replaces the search API pre-filter, which had ~28% false positives.

2. **Fetch each object:** `GET /objects/{id}` for each public domain ID
   - The API is behind Imperva WAF with a strict per-IP rate limit: ~80 requests per rolling 30-60s window
   - **With residential proxy rotation** (DataImpulse, $5 for 5 GB): each request goes through a different IP, bypassing the per-IP limit entirely. 150 concurrent requests via `asyncio.Semaphore`, sustained ~50 req/s
   - **~3 hours for all 248K objects** (~2.8% proxy connection errors, retried without proxy)
   - Without proxy (fallback): burst of 80 + 60s cooldown, ~1.33 req/s, ~52 hours

3. **Store all results:** Each fetched object's full JSON is stored in SQLite on receipt, with `is_public_domain` and `has_image` flags. 404s are stored with `data = NULL` to prevent re-fetching. This makes the crawl fully idempotent.

4. **Resume-safe:** If interrupted at any point, re-running skips all already-stored IDs and continues with the remainder.

**Rate limit details (Imperva WAF):**

The Met API documentation says "80 requests per second" but the actual limit is enforced by Imperva WAF at ~80 requests per rolling window per IP. Tested and confirmed:

| Technique | Result |
|---|---|
| Sequential requests (2 req/s) | Throttled after ~80 |
| Burst of 80 + 60s cooldown | Works reliably (~1.33 req/s) |
| Cloud VM (different IP) | Worse (20/30s = 0.67 req/s) |
| `curl_cffi` (Chrome TLS fingerprint) | Same limit |
| `cloudscraper` (WAF bypass) | Same limit |
| Playwright browser cookies | Same limit |
| **Residential proxy rotation (DataImpulse)** | **~50 req/s, zero throttling** |

The limit is IP-based at the network level. No single-IP technique bypasses it, but rotating residential proxies give each request a unique IP, making the per-IP limit irrelevant. DataImpulse residential proxies at $1/GB ($5 minimum for 5 GB) — the full 248K crawl uses ~750 MB.

**Output schema:**
```sql
CREATE TABLE raw_artworks (
  met_object_id INTEGER PRIMARY KEY,
  data TEXT,                     -- full JSON from API (NULL for 404s)
  is_public_domain BOOLEAN,
  has_image BOOLEAN,
  fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**Implementation:** `scripts/crawl.py` (adapted from `artalike/scripts/crawl.py`)

### Step 2: `caption.py` — Generate captions via Haiku 4.5 Batch

**Input:** Raw artwork data from Step 1 (`raw_artworks.db`)
**Output:** Captions + keywords stored in `captions.db`

1. Query `raw_artworks.db` for all verified public domain objects with images
2. For each artwork:
   - Build a Batch API request with the **thumbnail URL** (`primaryImageSmall`, typically 480-800px) + captioning prompt
   - Use `primaryImageSmall`, not `primaryImage` — thumbnails are sufficient for visual descriptions, cost ~640 tokens vs ~1,600 for full-res, and reduce CDN pressure from concurrent fetches
3. Submit batch(es) to Claude Haiku 4.5 Batch API
   - Batch API accepts up to 10,000 requests per batch
   - ~248K images = ~20 batches
4. Poll for completion (batches typically complete in minutes to hours)
5. Parse responses: extract `caption` and `keywords` from each
6. Store in `captions.db` keyed by `met_object_id`
7. Idempotent: skip already-captioned artworks on re-run

**No image downloads needed (primary approach).** The Claude Batch API supports Vision, and the Messages API supports URL-based image sources (`source.type: "url"`). Met CDN images are fetched server-side by Anthropic's infrastructure. Zero local image storage needed.

**Risk: concurrent CDN fetching.** The Batch API processes requests concurrently. 200K image URLs hitting Met CDN simultaneously could cause rate-limiting or fetch failures on individual requests. If this becomes a problem, the fallback is to download images locally and send them as base64-encoded data in the batch requests. This adds a download step (~50-100GB at 512px) but avoids CDN pressure.

**Budget:** ~$90-115 for ~248K images using thumbnails (Haiku 4.5 Batch: $0.50/MTok input, $2.50/MTok output). Using `primaryImageSmall` (~640 tokens/image) instead of `primaryImage` (~1,600 tokens/image) roughly halves the cost.

### Step 3: `build_db.py` — Build SQLite database

**Input:** Raw artwork data + captions from Steps 1-2
**Output:** `artworks.db`

1. Create SQLite database with schema from Section 5
2. For each artwork:
   - Extract structured fields from Met API JSON (title, artist, dates, medium, etc.)
   - Insert caption + keywords from Step 2
   - Insert image URLs (primaryImage, primaryImageSmall)
3. Build FTS5 index
4. Optimize and vacuum
5. Output: single `artworks.db` file ready for deployment

---

## 10. Deployment

### Container

```dockerfile
FROM node:20-slim
WORKDIR /app
COPY package*.json ./
RUN npm ci --production
COPY dist/ ./dist/
COPY artworks.db ./data/
EXPOSE 3001
CMD ["node", "dist/server.js"]
```

The SQLite DB (~50-100MB) ships inside the container image. No persistent volumes needed since the DB is read-only and rebuilt from the pipeline.

### Platform comparison

| Platform | $/month | Cold Start | Scale-to-Zero | HTTPS | Notes |
|---|---|---|---|---|---|
| **Fly.io** | ~$1-5 | <1s | Yes (native) | Yes | Best cold start. No free tier for new accounts. |
| **Railway** | $5 flat | ~1-5s | Yes | Yes | $5 subscription includes $5 usage credit. |
| **Render (free)** | $0 | **30-60s** | Yes | Yes | Free but cold start may cause MCP client timeouts. |
| **Render (paid)** | $7 | None | No (always on) | Yes | No sleep, no cold start. |
| **Hetzner VPS** | ~$3.80 | None | No (always on) | DIY | Best value (2 vCPU, 4GB RAM). Must set up Caddy for TLS. |
| **DO App Platform** | $5 | None | No | Yes | Straightforward but no scale-to-zero. |

### Recommended: Fly.io

- **Sub-second cold start** with native auto-stop/auto-start — critical for an MCP server where clients expect quick responses
- Direct SQLite support (DB ships in container, no volume needed for read-only)
- Pay only for active time; could be <$1/month at low traffic
- HTTPS included with automatic TLS

**Runner-up: Hetzner VPS** ($3.80/month) if you prefer always-on with zero cold start and don't mind managing TLS via Caddy. Massively more resources for the price (2 vCPU, 4GB RAM, 40GB NVMe).

**Avoid: Render free tier** — 30-60 second cold start is likely to cause MCP protocol timeouts.

### Update process

```bash
# Re-run pipeline
python scripts/crawl.py
python scripts/caption.py
python scripts/build_db.py

# Rebuild and deploy
npm run build
# copy artworks.db into container
fly deploy
```

---

## 11. Project Structure

```
curator/
├── SPEC.md                    # This file
├── package.json
├── tsconfig.json
├── vite.config.ts
├── server.ts                  # MCP server (tools + resources + Express)
├── carousel.html              # Artwork viewer UI entry point
├── src/
│   └── carousel.ts            # Artwork viewer logic (App class, image rendering)
├── scripts/                   # Data pipeline (Python)
│   ├── crawl.py               # Step 1: Crawl Met API
│   ├── caption.py             # Step 2: Caption via Haiku Batch
│   └── build_db.py            # Step 3: Build SQLite DB
├── data/
│   └── artworks.db            # Built by pipeline (not in git)
└── dist/                      # Built assets (not in git)
    ├── server.js
    └── carousel.html          # Bundled single-file HTML
```

---

## 12. Decisions Log

| Decision | Choice | Reasoning |
|---|---|---|
| Architecture | Monolith (Node.js + SQLite) | Fastest to ship, zero external deps, sufficient for 248K rows |
| Database | SQLite + FTS5 | In-process (fastest), FTS5 for text search, read-only workload |
| Search approach | Claude writes raw SQL | Most flexible — handles keyword, filter, aggregate, and iterative queries |
| Captioning model | Haiku 4.5 Batch | ~$180 for all 248K images, well within $500 budget |
| Caption style | Visual description + keywords | Grounded in what's visible, keywords boost search coverage |
| Museum source | Met Museum only (V1) | Public domain images, English metadata, no auth, existing crawler |
| Similarity search | Dropped | Full pivot to "Claude as curator" — Claude's intelligence replaces embeddings |
| Vector database | Not needed | FTS5 + Claude's keyword intelligence is sufficient for V1 |
| UI paradigm | Single full-width image per tool call | Simplest possible — one image per iframe, Claude handles all text |
| Interaction model | Claude-only | No in-app controls. All queries and commentary go through Claude. |
| Multiple images | Multiple tool calls | Claude calls `show_artwork` once per image, writes artist/date as text |
| Auth | None (public) | All data is public domain CC0. Read-only access. |
| SQL safety | Read-only + timeout | DB is read-only, 5s timeout, 100-row cap, SELECT-only validation |
| French text (Louvre) | Dropped Louvre | Met-only eliminates the language problem entirely |
| Data pipeline language | Python (separate from runtime) | Batch jobs; Python has better ML/data tooling |
| Pipeline stages | Separate steps | Each step re-runnable independently |
| Crawl strategy | CSV filter + API fetch + residential proxy | CSV gives exact 248K public domain IDs (zero false positives vs 28% from search API). API only needed for image URLs. Residential proxy rotation bypasses Imperva WAF per-IP limit: ~3 hours instead of ~52. |
| Image storage | URLs only, no downloads | Claude API accepts image URLs directly for captioning. Runtime viewer loads from Met CDN. Zero image storage. |
| Deployment | Fly.io (recommended) | Sub-second cold start with auto-stop. Best fit for low-traffic MCP server. Hetzner VPS as runner-up. |
| Naming | Deferred | Ship first, name later |

---

## 13. Not in V1 (Future Iteration)

- **Additional museums:** Art Institute of Chicago, Cleveland Museum of Art, Harvard Art Museums
- **Vector/embedding search:** Add if FTS5 + Claude intelligence proves insufficient
- **User authentication / favorites / collections**
- **In-app UI controls** (search bar, filters, "more like this")
- **Louvre integration** (requires French → English translation pipeline)
- **Connectors directory listing** (requires meeting Anthropic quality standards)
- **Text-to-image search via SigLip2** (original artalike had this potential)
- **Richer viewer** (detail view, zoom, museum link, related works)

---

## 14. Implementation Order

Crawl is complete (248K objects, ~3 hours via residential proxy). No longer the critical path.

### Phase 1: Crawl ✅

```
1. Write crawl.py                              ✅ Done
2. Run crawl (~3 hrs via residential proxy)     ✅ Done — 248,472 rows, 243,054 valid
```

### Phase 2: Server + UI ✅

```
3. Scaffold Node.js project                     ✅ Done
4. Write build_db.py                             ✅ Done
5. Build test DB (243,054 artworks, 155 MB)      ✅ Done
6. Implement query_artworks tool                 ✅ Done
7. Implement show_artwork tool                   ✅ Done
8. Build artwork viewer UI                       ✅ Done
9. Test end-to-end (cloudflared + Claude)         ✅ Done
```

### Phase 3: Captioning

```
10. Write caption.py
11. Run Haiku 4.5 Batch on all crawled artworks (~243K valid)
    (hours, runs in background)
12. Fix any caption parsing issues
```

### Phase 4: Full build + deploy

```
13. Rebuild artworks.db with captions + FTS5
14. Re-test with full dataset
15. Deploy to Fly.io
16. Add as custom connector in Claude
17. End-to-end smoke test in Claude
```

### Why this order

- **Test DB early:** Don't wait for captions to start building the server. The full crawl data with just metadata (no captions) is enough to develop and test the MCP tools and carousel UI.
- **Caption last:** Captioning is expensive ($90-115) and the prompt may need iteration. Get the server working first so you can evaluate caption quality in context.
- **Deploy after captioning:** The full DB with captions is needed for a meaningful deployment.

---

## 15. Open Questions (to resolve during implementation)

- **Captioning batch size:** How to chunk 243K images into Batch API requests
- **FTS5 tokenizer:** Default vs porter stemming vs unicode61
- **Deployment region:** Fly.io region selection (US-east to minimize latency to Anthropic's servers)

### Resolved

- **UI design:** Single full-width image per tool call. Claude writes all text (artist, date, commentary). No carousel/cards.
- **Full image handling:** UI shows `image_url` (full res) with fallback to `thumbnail_url`. Both from Met CDN.
- **Context updates:** Not needed — each `show_artwork` call returns full metadata as text to Claude.
- **CSP for images:** Use `resourceDomains` (not `connectDomains`) to allowlist Met CDN.
- **registerAppTool vs server.tool:** `query_artworks` (no UI) uses `server.tool()` with Zod schemas. `show_artwork` (with UI) uses `registerAppTool` which requires `_meta.ui.resourceUri`.
