import express from "express";
import cors from "cors";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  registerAppTool,
  registerAppResource,
  RESOURCE_MIME_TYPE,
} from "@modelcontextprotocol/ext-apps/server";
import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";
import { z } from "zod";

const PORT = parseInt(process.env.PORT || "3001", 10);
const DB_PATH = path.join(import.meta.dirname, "data", "artworks.db");
const DIST_DIR = path.join(import.meta.dirname, "dist");

// Open DB read-only
const db = new Database(DB_PATH, { readonly: true });

const MAX_ROWS = 100;
const resourceUri = "ui://art-curator/viewer.html";

function createServer(): McpServer {
  const server = new McpServer(
    {
      name: "Art Curator",
      version: "1.0.0",
    },
    {
      instructions: `You are an art curator assistant with access to a database of 243,000+ artworks from the Metropolitan Museum of Art.

When showing artworks, display each artwork's info (title, artist, date, medium, etc.) immediately after its image using show_artwork, rather than batching all details at the end. Interleave images and descriptions so the user sees context for each piece as they view it.`,
    }
  );

  // --- Tool: query_artworks (no UI) ---
  const queryDescription = `Execute a read-only SQL query against the artworks database.

Schema:
  artworks (
    id INTEGER PRIMARY KEY,
    met_object_id INTEGER UNIQUE,
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
    description TEXT,           -- curatorial essay from Met website (NULL for 59% of artworks)
    image_url TEXT,             -- full resolution (Met CDN)
    thumbnail_url TEXT,         -- web-size (Met CDN)
    object_url TEXT,            -- link to Met museum page
    credit_line TEXT,
    accession_number TEXT,
    gallery_number TEXT,
    is_highlight BOOLEAN
  )

  artworks_fts (FTS5 virtual table over: title, artist_name, medium, caption, description, culture, period, department)
  NOTE: artworks_fts does NOT have id or other columns from artworks. Always JOIN to get full data:
    SELECT a.* FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid WHERE artworks_fts MATCH '...' LIMIT 20;

IMPORTANT: Prefer FTS over LIKE for text searches — it is 100x+ faster. Use column-scoped FTS for targeted searches (e.g. artist_name:Vermeer).
  Avoid: SELECT * FROM artworks WHERE artist_name LIKE '%Vermeer%' (full table scan, ~60ms)
  Use:   SELECT a.* FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid WHERE artworks_fts MATCH 'artist_name:Vermeer' LIMIT 20; (~0.4ms)

Example queries:
  SELECT a.* FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid WHERE artworks_fts MATCH 'artist_name:Vermeer' LIMIT 20;
  SELECT a.* FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid WHERE artworks_fts MATCH 'storm AND ship' LIMIT 20;
  SELECT * FROM artworks WHERE date_begin >= 1800 AND date_end <= 1899 AND department = 'European Paintings' LIMIT 20;
  SELECT COUNT(*) as count, department FROM artworks GROUP BY department ORDER BY count DESC;

Return results as JSON. Max ${MAX_ROWS} rows per query.`;

  server.tool(
    "query_artworks",
    queryDescription,
    { sql: z.string().describe("Read-only SQL query (SELECT only)") },
    async ({ sql }) => {
      try {
        const normalized = sql.trim().replace(/\s+/g, " ");
        if (!normalized.toUpperCase().startsWith("SELECT")) {
          return {
            content: [
              { type: "text" as const, text: "Error: Only SELECT queries are allowed." },
            ],
          };
        }

        // Reject LIKE on FTS-indexed columns — FTS MATCH is 100x faster
        const FTS_COLUMNS = ["title", "artist_name", "medium", "caption", "description", "culture", "period", "department"];
        const likeMatch = normalized.match(/(?:\.)?(\w+)\s+LIKE\s+/i);
        if (likeMatch && FTS_COLUMNS.includes(likeMatch[1])) {
          return {
            content: [
              { type: "text" as const, text: `Error: LIKE on "${likeMatch[1]}" is too slow (full table scan). Use FTS instead:\n  SELECT a.* FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid WHERE artworks_fts MATCH '${likeMatch[1]}:search_term' LIMIT 20;` },
            ],
          };
        }

        const stmt = db.prepare(sql);
        const rows = stmt.all().slice(0, MAX_ROWS);

        return {
          content: [
            { type: "text" as const, text: JSON.stringify(rows, null, 2) },
          ],
        };
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        return {
          content: [
            { type: "text" as const, text: `SQL Error: ${message}` },
          ],
        };
      }
    }
  );

  // --- Tool: show_artwork (single image with UI) ---
  registerAppTool(
    server,
    "show_artwork",
    {
      title: "Show Artwork",
      description:
        "Display a single artwork image inline. Call query_artworks first to find artworks, then pass one ID here. Call multiple times to show multiple images. IMPORTANT: After each show_artwork call, immediately describe that artwork (title, artist, date, medium, etc.) before calling show_artwork again. Interleave images and descriptions — never batch all descriptions at the end.",
      inputSchema: z.object({
        id: z.number().int().describe("Artwork ID to display"),
      }),
      _meta: { ui: { resourceUri } },
    },
    async (args: { id: number }) => {
      const row = db
        .prepare("SELECT * FROM artworks WHERE id = ?")
        .get(args.id) as Record<string, unknown> | undefined;

      if (!row) {
        return {
          content: [
            { type: "text" as const, text: `Artwork ID ${args.id} not found.` },
          ],
        };
      }

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(row, null, 2),
          },
        ],
        structuredContent: {
          artwork: row,
        },
      };
    }
  );

  // --- Resource: artwork viewer HTML ---
  registerAppResource(
    server,
    "Artwork Viewer",
    resourceUri,
    { mimeType: RESOURCE_MIME_TYPE },
    async () => {
      const htmlPath = path.join(DIST_DIR, "viewer.html");
      const html = fs.readFileSync(htmlPath, "utf-8");
      return {
        contents: [
          {
            uri: resourceUri,
            mimeType: RESOURCE_MIME_TYPE,
            text: html,
            _meta: {
              ui: {
                csp: {
                  resourceDomains: ["https://images.metmuseum.org"],
                },
              },
            },
          },
        ],
      };
    }
  );

  return server;
}

// --- Express + MCP transport ---
const app = express();
app.use(cors());
app.use(express.json());

app.post("/mcp", async (req, res) => {
  const server = createServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined, // stateless
  });
  res.on("close", () => transport.close());
  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);
});

app.get("/health", (_req, res) => {
  res.json({ status: "ok" });
});

app.listen(PORT, () => {
  console.log(`Art Curator MCP server listening on port ${PORT}`);
  console.log(`  DB: ${DB_PATH}`);
  console.log(`  MCP endpoint: POST http://localhost:${PORT}/mcp`);
});

process.on("SIGTERM", () => { db.close(); process.exit(0); });
process.on("SIGINT", () => { db.close(); process.exit(0); });
