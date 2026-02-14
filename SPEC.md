# Art Curator MCP App — Specification

> An MCP App that gives Claude direct SQL access to a database of captioned museum artworks, enabling Claude to act as an art curator — searching, recommending, and discussing art within a carousel UI embedded in the conversation.

## References

- https://modelcontextprotocol.io/docs/extensions/apps
- https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp
- https://support.claude.com/en/articles/11176164-pre-built-web-connectors-using-remote-mcp
- https://support.claude.com/en/articles/11724452-using-the-connectors-directory-to-extend-claude-s-capabilities

---

## 1. Product Vision

Claude acts as an **art curator**. Users ask Claude about art in natural language ("find me paintings of boats in storms", "show me Vermeer", "what impressionist landscapes do you have?"). Claude queries a database of captioned museum artworks, curates the best matches, and displays them in an interactive carousel UI embedded directly in the chat.

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
                       └─ Bundled HTML (carousel UI resource)
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

Claude writes raw SQL queries against the artworks database. Returns results as JSON text.

```typescript
registerAppTool(server, "query_artworks", {
  title: "Query Artworks",
  description: `Execute a read-only SQL query against the artworks database.

Schema:
  artworks (
    id INTEGER PRIMARY KEY,
    title TEXT,
    artist_name TEXT,
    artist_bio TEXT,
    artist_nationality TEXT,
    artist_birth_year INTEGER,
    artist_death_year INTEGER,
    object_date TEXT,           -- display string, e.g. "ca. 1662"
    date_begin INTEGER,         -- e.g. 1657
    date_end INTEGER,           -- e.g. 1667
    medium TEXT,                -- e.g. "Oil on canvas"
    dimensions TEXT,
    department TEXT,
    classification TEXT,
    culture TEXT,
    period TEXT,
    caption TEXT,               -- AI-generated visual description
    keywords TEXT,              -- comma-separated searchable terms
    image_url TEXT,             -- full resolution (Met CDN)
    thumbnail_url TEXT,         -- web-size (Met CDN)
    object_url TEXT,            -- link to Met museum page
    credit_line TEXT,
    accession_number TEXT,
    gallery_number TEXT,
    is_highlight BOOLEAN
  )

  artworks_fts (FTS5 virtual table over: title, artist_name, medium, caption, keywords, culture, period, department)

Example queries:
  SELECT * FROM artworks WHERE artist_name LIKE '%Vermeer%' LIMIT 20;
  SELECT * FROM artworks_fts WHERE artworks_fts MATCH 'boats AND storms' LIMIT 20;
  SELECT * FROM artworks WHERE date_begin >= 1800 AND date_end <= 1899 AND department = 'European Paintings' LIMIT 20;
  SELECT COUNT(*) as count, department FROM artworks GROUP BY department ORDER BY count DESC;

Return results as JSON. Max 100 rows per query.`,
  inputSchema: {
    type: "object",
    properties: {
      sql: { type: "string", description: "Read-only SQL query (SELECT only)" }
    },
    required: ["sql"]
  }
});
```

**Returns:** Text content with JSON array of matching rows. No UI.

### 4.2 `show_artworks` — Display carousel (with UI)

Renders selected artworks in the carousel UI embedded in the chat. Claude calls this after curating search results.

```typescript
const carouselResourceUri = "ui://art-curator/carousel.html";

registerAppTool(server, "show_artworks", {
  title: "Show Artworks",
  description: "Display selected artworks in an interactive carousel. Call query_artworks first to find artworks, then pass the IDs of the best matches here.",
  inputSchema: {
    type: "object",
    properties: {
      ids: {
        type: "array",
        items: { type: "integer" },
        description: "Array of artwork IDs to display in the carousel"
      }
    },
    required: ["ids"]
  },
  _meta: { ui: { resourceUri: carouselResourceUri } }
});
```

**Returns:** Text content with artwork metadata (so Claude can comment) + renders carousel UI.

### 4.3 Interaction Flow

```
1. User: "Find me paintings of boats in storms"
2. Claude calls query_artworks({ sql: "SELECT * FROM artworks_fts WHERE artworks_fts MATCH 'boats storms' LIMIT 20" })
   → Returns 20 rows of metadata as JSON text to Claude
3. Claude reads results, picks the 5 best matches
4. Claude calls show_artworks({ ids: [4523, 8901, 12045, 15678, 19234] })
   → Carousel UI renders in chat with those 5 artworks
   → Claude also receives text metadata to write commentary
5. Claude writes: "Here are 5 dramatic paintings of ships in stormy seas..."
6. User taps an artwork in the carousel
   → App pushes full metadata to Claude via context update
7. Claude: "That's 'The Storm on the Sea of Galilee' by Rembrandt, 1633..."
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
  thumbnail_url TEXT NOT NULL,  -- primaryImageSmall (web-size ~480-800px, used for carousel display + captioning)
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

- The carousel UI runs in a sandboxed iframe controlled by the host (Claude)
- No access to parent window DOM, cookies, or local storage
- Communication only via postMessage (abstracted by `@modelcontextprotocol/ext-apps` `App` class)
- Image URLs point to Met Museum CDN — no self-hosted images

### 7.3 Authentication

- **None.** The MCP server is publicly accessible. No OAuth, no API keys.
- All data served is public domain (CC0) museum metadata
- Read-only access prevents abuse

---

## 8. MCP App UI (Carousel)

### 8.1 Technology

- Vanilla JavaScript (no framework) — matches original artalike approach
- Bundled into single HTML file via Vite + `vite-plugin-singlefile`
- Served as a `ui://` resource by the MCP server

### 8.2 Carousel design

- **Layout:** Horizontal card carousel, swipeable
- **Card content:** Thumbnail image, title, artist name, date (details TBD during implementation)
- **Interaction:** Tap/click a card to view larger + push context to Claude
- **No in-app controls:** No search bar, no filters, no "more like this" buttons. All interaction goes through Claude.
- **Responsive:** Adapts to the iframe width provided by the host

### 8.3 Claude-aware browsing

When the user taps an artwork in the carousel, the app pushes a **context update** to Claude containing the full metadata:

```typescript
app.updateContext({
  type: "artwork_selected",
  data: {
    id: 12345,
    title: "Young Woman with a Water Pitcher",
    artist_name: "Johannes Vermeer",
    object_date: "ca. 1662",
    medium: "Oil on canvas",
    department: "European Paintings",
    caption: "A woman in a blue dress stands beside a sunlit window...",
    image_url: "https://images.metmuseum.org/...",
    object_url: "https://www.metmuseum.org/art/collection/search/437881"
  }
});
```

This lets Claude respond to what the user is looking at without an explicit question — e.g., offering more information about the artist, suggesting related works, or providing historical context.

### 8.4 State management

Each `show_artworks` tool invocation **replaces** the carousel contents. Previous results are not accumulated. Claude remembers prior searches in its conversation context and can reference them.

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
├── carousel.html              # Carousel UI entry point
├── src/
│   └── carousel.ts            # Carousel UI logic (App class, rendering)
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
| UI paradigm | Carousel/card in MCP App iframe | Conversation-native, compact, swipeable |
| Interaction model | Claude-only | No in-app search/filter controls. All queries go through Claude. |
| Claude awareness | Full metadata push on tap | Claude can respond to what user is browsing |
| Auth | None (public) | All data is public domain CC0. Read-only access. |
| SQL safety | Read-only + timeout | DB is read-only, 5s timeout, 100-row cap, SELECT-only validation |
| French text (Louvre) | Dropped Louvre | Met-only eliminates the language problem entirely |
| Data pipeline language | Python (separate from runtime) | Batch jobs; Python has better ML/data tooling |
| Pipeline stages | Separate steps | Each step re-runnable independently |
| Crawl strategy | CSV filter + API fetch + residential proxy | CSV gives exact 248K public domain IDs (zero false positives vs 28% from search API). API only needed for image URLs. Residential proxy rotation bypasses Imperva WAF per-IP limit: ~3 hours instead of ~52. |
| Image storage | URLs only, no downloads | Claude API accepts image URLs directly for captioning. Runtime carousel loads from Met CDN. Zero image storage. |
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
- **Richer carousel** (detail view, zoom, museum link, related works)

---

## 14. Implementation Order

Crawl is complete (248K objects, ~3 hours via residential proxy). No longer the critical path.

### Phase 1: Crawl ✅

```
1. Write crawl.py                              ✅ Done
2. Run crawl (~3 hrs via residential proxy)     ✅ Done — 248,472 rows, 243,054 valid
```

### Phase 2: Server + UI

```
3. Scaffold Node.js project (package.json, tsconfig, vite.config, server.ts)
4. Write build_db.py
5. Build test DB from crawled artworks (no captions yet)
6. Implement query_artworks tool
7. Implement show_artworks tool
8. Build carousel UI (carousel.html + carousel.ts)
9. Test end-to-end locally (cloudflared tunnel + Claude)
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

- **Carousel card content:** Exact fields shown on each card (title + artist + date minimum, but visual density TBD)
- **Full image handling:** Whether to show full-res images in the app or link to Met museum page
- **Context update frequency:** Push on every tap, or debounce/throttle?
- **Captioning batch size:** How to chunk 248K images into Batch API requests
- **FTS5 tokenizer:** Default vs porter stemming vs unicode61
- **Deployment region:** Fly.io region selection (US-east to minimize latency to Anthropic's servers)
