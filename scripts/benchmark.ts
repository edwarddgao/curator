import Database from "better-sqlite3";
import path from "node:path";
import fs from "node:fs";
import { parseArgs } from "node:util";
import { performance } from "node:perf_hooks";

// --- CLI args ---
const { values } = parseArgs({
  options: {
    iterations: { type: "string", short: "n", default: "100" },
    warmup: { type: "string", short: "w", default: "5" },
    verbose: { type: "boolean", short: "v", default: false },
    query: { type: "string", short: "q" },
  },
});

const ITERATIONS = parseInt(values.iterations!, 10);
const WARMUP_RUNS = parseInt(values.warmup!, 10);
const VERBOSE = values.verbose!;
const QUERY_FILTER = values.query ?? null;

// --- Types ---
interface BenchmarkResult {
  name: string;
  cold: number;
  min: number;
  max: number;
  mean: number;
  median: number;
  p95: number;
  p99: number;
  stddev: number;
  rowsReturned: number;
  queryPlan: string[];
}

interface QueryDef {
  name: string;
  sql: string;
  params: () => unknown[];
  mode: "get" | "all";
}

// --- DB setup (mirrors server.ts) ---
const DB_PATH = path.join(import.meta.dirname, "..", "data", "artworks.db");
const db = new Database(DB_PATH, { readonly: true });
db.pragma("journal_mode = WAL");
db.pragma("busy_timeout = 5000");

// --- Helpers ---
function randInt(lo: number, hi: number): number {
  return Math.floor(Math.random() * (hi - lo + 1)) + lo;
}

function pick<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

function timeExec(
  stmt: Database.Statement,
  params: unknown[],
  mode: "get" | "all"
): { ms: number; rows: number } {
  const start = performance.now();
  let rows: number;
  if (mode === "get") {
    const result = stmt.get(...params);
    rows = result ? 1 : 0;
  } else {
    const results = stmt.all(...params);
    rows = results.length;
  }
  return { ms: performance.now() - start, rows };
}

function computeStats(timings: number[]) {
  const sorted = [...timings].sort((a, b) => a - b);
  const n = sorted.length;
  const sum = sorted.reduce((a, b) => a + b, 0);
  const mean = sum / n;
  const variance = sorted.reduce((acc, v) => acc + (v - mean) ** 2, 0) / n;
  return {
    min: sorted[0],
    max: sorted[n - 1],
    mean,
    median: sorted[Math.floor(n / 2)],
    p95: sorted[Math.floor(n * 0.95)],
    p99: sorted[Math.floor(n * 0.99)],
    stddev: Math.sqrt(variance),
  };
}

function pad(s: string, width: number, align: "left" | "right" = "right") {
  return align === "left" ? s.padEnd(width) : s.padStart(width);
}

function fmt(n: number, decimals = 3) {
  return n.toFixed(decimals);
}

function formatTable(results: BenchmarkResult[]): string {
  const cols = [
    { header: "Query", width: 24, align: "left" as const },
    { header: "Cold", width: 10, align: "right" as const },
    { header: "Min", width: 10, align: "right" as const },
    { header: "Mean", width: 10, align: "right" as const },
    { header: "Median", width: 10, align: "right" as const },
    { header: "P95", width: 10, align: "right" as const },
    { header: "P99", width: 10, align: "right" as const },
    { header: "Max", width: 10, align: "right" as const },
    { header: "StdDev", width: 10, align: "right" as const },
    { header: "Rows", width: 6, align: "right" as const },
  ];

  const headerLine = cols.map((c) => pad(c.header, c.width, c.align)).join("  ");
  const sep = "-".repeat(headerLine.length);
  const lines = [headerLine, sep];

  for (const r of results) {
    const row = [
      pad(r.name, cols[0].width, "left"),
      pad(fmt(r.cold), cols[1].width),
      pad(fmt(r.min), cols[2].width),
      pad(fmt(r.mean), cols[3].width),
      pad(fmt(r.median), cols[4].width),
      pad(fmt(r.p95), cols[5].width),
      pad(fmt(r.p99), cols[6].width),
      pad(fmt(r.max), cols[7].width),
      pad(fmt(r.stddev), cols[8].width),
      pad(String(r.rowsReturned), cols[9].width),
    ];
    lines.push(row.join("  "));
  }

  return lines.join("\n");
}

// --- Parameter pools ---
const DEPARTMENTS = [
  "Drawings and Prints",
  "European Sculpture and Decorative Arts",
  "Asian Art",
  "Greek and Roman Art",
  "Islamic Art",
  "Egyptian Art",
  "The American Wing",
  "Costume Institute",
  "Arms and Armor",
  "Medieval Art",
  "Photographs",
  "European Paintings",
];

const LIKE_PATTERNS = [
  "%Rembrandt%", "%Dürer%", "%Callot%", "%Rowlandson%",
  "%Daumier%", "%Hiroshige%", "%Vermeer%", "%Monet%",
  "%della Bella%", "%van Gogh%",
];

const FTS_SIMPLE_ARTISTS = [
  "Rembrandt", "Dürer", "Callot", "Rowlandson",
  "Daumier", "Hiroshige", "Vermeer", "Monet",
  "Bella", "Gogh",
];

const FTS_SIMPLE_TERMS = [
  "portrait", "landscape", "bronze", "marble", "sword",
  "ceramic", "silk", "gold", "temple", "flower",
];

const FTS_BOOLEAN_QUERIES = [
  "landscape AND oil", "portrait AND woman", "gold AND silver",
  "warrior NOT japanese", "silk OR satin", "bronze AND greek",
  "painting AND italian", "temple AND stone", "flower AND vase",
  "horse AND battle",
];

const FTS_COMPLEX = [
  { match: "portrait AND oil", dept: "European Paintings", dateBegin: 1600, dateEnd: 1900 },
  { match: "landscape", dept: "European Paintings", dateBegin: 1700, dateEnd: 1900 },
  { match: "sword AND armor", dept: "Arms and Armor", dateBegin: 1400, dateEnd: 1700 },
  { match: "ceramic AND blue", dept: "Asian Art", dateBegin: 1300, dateEnd: 1800 },
  { match: "gold AND ornament", dept: "Egyptian Art", dateBegin: -2000, dateEnd: 0 },
  { match: "marble AND figure", dept: "Greek and Roman Art", dateBegin: -500, dateEnd: 300 },
  { match: "silk AND embroidered", dept: "Asian Art", dateBegin: 1600, dateEnd: 1900 },
  { match: "print AND satire", dept: "Drawings and Prints", dateBegin: 1700, dateEnd: 1850 },
];

// --- Pre-fetch ID bounds ---
const MAX_ID = (db.prepare("SELECT MAX(id) as m FROM artworks").get() as any).m;
const MAX_MET_ID = (db.prepare("SELECT MAX(met_object_id) as m FROM artworks").get() as any).m;

// --- Query definitions ---
const BENCHMARKS: QueryDef[] = [
  {
    name: "pk_lookup",
    sql: "SELECT * FROM artworks WHERE id = ?",
    params: () => [randInt(1, MAX_ID)],
    mode: "get",
  },
  {
    name: "unique_idx_lookup",
    sql: "SELECT * FROM artworks WHERE met_object_id = ?",
    params: () => [randInt(1, MAX_MET_ID)],
    mode: "get",
  },
  {
    name: "like_artist_name",
    sql: "SELECT * FROM artworks WHERE artist_name LIKE ? LIMIT 100",
    params: () => [pick(LIKE_PATTERNS)],
    mode: "all",
  },
  {
    name: "fts_artist_name",
    sql: `SELECT a.* FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid
          WHERE artworks_fts MATCH ? LIMIT 100`,
    params: () => [`artist_name:${pick(FTS_SIMPLE_ARTISTS)}`],
    mode: "all",
  },
  {
    name: "dept_exact",
    sql: "SELECT * FROM artworks WHERE department = ? LIMIT 100",
    params: () => [pick(DEPARTMENTS)],
    mode: "all",
  },
  {
    name: "date_range",
    sql: "SELECT * FROM artworks WHERE date_begin >= ? AND date_end <= ? LIMIT 100",
    params: () => {
      const start = randInt(1400, 1900);
      return [start, start + randInt(50, 200)];
    },
    mode: "all",
  },
  {
    name: "fts_simple",
    sql: `SELECT a.id, a.title, a.artist_name, a.object_date, a.medium, a.thumbnail_url
          FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid
          WHERE artworks_fts MATCH ? LIMIT 20`,
    params: () => [pick(FTS_SIMPLE_TERMS)],
    mode: "all",
  },
  {
    name: "fts_boolean",
    sql: `SELECT a.id, a.title, a.artist_name, a.object_date, a.medium, a.thumbnail_url
          FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid
          WHERE artworks_fts MATCH ? LIMIT 20`,
    params: () => [pick(FTS_BOOLEAN_QUERIES)],
    mode: "all",
  },
  {
    name: "fts_with_filters",
    sql: `SELECT a.id, a.title, a.artist_name, a.object_date, a.medium, a.thumbnail_url
          FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid
          WHERE artworks_fts MATCH ? AND a.department = ? AND a.date_begin >= ? LIMIT 20`,
    params: () => {
      const q = pick(FTS_COMPLEX);
      return [q.match, q.dept, q.dateBegin];
    },
    mode: "all",
  },
  {
    name: "aggregation",
    sql: "SELECT COUNT(*) as count, department FROM artworks GROUP BY department ORDER BY count DESC",
    params: () => [],
    mode: "all",
  },
  {
    name: "complex_combined",
    sql: `SELECT a.id, a.title, a.artist_name, a.object_date, a.date_begin, a.medium, a.department, a.thumbnail_url
          FROM artworks a JOIN artworks_fts f ON a.rowid = f.rowid
          WHERE artworks_fts MATCH ? AND a.department = ? AND a.date_begin >= ? AND a.date_end <= ?
          ORDER BY a.date_begin ASC LIMIT 20`,
    params: () => {
      const q = pick(FTS_COMPLEX);
      return [q.match, q.dept, q.dateBegin, q.dateEnd];
    },
    mode: "all",
  },
];

// --- Runner ---
function runBenchmark(queryDef: QueryDef): BenchmarkResult {
  // Cold run (fresh prepared statement)
  const coldStmt = db.prepare(queryDef.sql);
  const coldResult = timeExec(coldStmt, queryDef.params(), queryDef.mode);

  // Warm-up
  const stmt = db.prepare(queryDef.sql);
  for (let i = 0; i < WARMUP_RUNS; i++) {
    if (queryDef.mode === "get") stmt.get(...queryDef.params());
    else stmt.all(...queryDef.params());
  }

  // Measured iterations
  const timings: number[] = [];
  let lastRowCount = 0;
  for (let i = 0; i < ITERATIONS; i++) {
    const params = queryDef.params();
    const { ms, rows } = timeExec(stmt, params, queryDef.mode);
    timings.push(ms);
    lastRowCount = rows;
    if (VERBOSE) {
      console.log(`  ${queryDef.name} [${i + 1}/${ITERATIONS}]: ${ms.toFixed(3)} ms (${rows} rows)`);
    }
  }

  // Query plan
  const planStmt = db.prepare(`EXPLAIN QUERY PLAN ${queryDef.sql}`);
  const sampleParams = queryDef.params();
  const planRows = planStmt.all(...sampleParams) as { detail: string }[];
  const queryPlan = planRows.map((r) => r.detail);

  return {
    name: queryDef.name,
    cold: coldResult.ms,
    ...computeStats(timings),
    rowsReturned: lastRowCount,
    queryPlan,
  };
}

// --- Main ---
const totalRows = (db.prepare("SELECT COUNT(*) as n FROM artworks").get() as any).n;
const sqliteVersion = (db.prepare("SELECT sqlite_version() as v").get() as any).v;
const dbSizeMB = fs.statSync(DB_PATH).size / (1024 * 1024);

console.log("Art Curator DB Benchmark");
console.log("========================");
console.log(`Database: ${path.relative(process.cwd(), DB_PATH)} (${dbSizeMB.toFixed(1)} MB, ${totalRows.toLocaleString()} rows)`);
console.log(`Iterations: ${ITERATIONS} (+ ${WARMUP_RUNS} warmup)`);
console.log(`Node: ${process.version} | SQLite: ${sqliteVersion}\n`);

const benchmarks = QUERY_FILTER
  ? BENCHMARKS.filter((b) => b.name === QUERY_FILTER)
  : BENCHMARKS;

if (benchmarks.length === 0) {
  console.error(`Unknown query: "${QUERY_FILTER}"`);
  console.error(`Available: ${BENCHMARKS.map((b) => b.name).join(", ")}`);
  process.exit(1);
}

const results: BenchmarkResult[] = [];
for (const queryDef of benchmarks) {
  process.stdout.write(`Running ${queryDef.name}...`);
  const result = runBenchmark(queryDef);
  process.stdout.write(` done (mean: ${result.mean.toFixed(3)} ms)\n`);
  results.push(result);
}

console.log(`\nAll times in milliseconds:\n`);
console.log(formatTable(results));

console.log("\nQuery Plans:");
for (const r of results) {
  console.log(`  ${r.name}:`);
  for (const line of r.queryPlan) {
    console.log(`    ${line}`);
  }
}

db.close();
