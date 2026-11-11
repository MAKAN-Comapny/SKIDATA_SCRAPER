/**
 * Node.js unit tests for Vercel Serverless APIs (`api/status.js` and `api/execute.js`).
 */

import test from "node:test";
import assert from "node:assert/strict";
import zlib from "node:zlib";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

const statusHandler = require(path.join(__dirname, "..", "api", "status.js"));
const executeHandler = require(path.join(__dirname, "..", "api", "execute.js"));

/**
 * Build a minimal valid ZIP Buffer (optionally simulating GitHub Actions Data Descriptors
 * where Local File Header sizes = 0 and Central Directory holds real sizes).
 */
function buildSingleFileZip(filename, contentStr, { method = 8, useDataDescriptor = true } = {}) {
  const nameBuf = Buffer.from(filename, "utf8");
  const rawData = Buffer.from(contentStr, "utf8");
  const payload = method === 8 ? zlib.deflateRawSync(rawData) : rawData;
  const crc = 0; // Not checked by inflateRawSync/CentralDirectory reader

  const flags = useDataDescriptor ? 0x0008 : 0x0000;
  const lfhSize = useDataDescriptor ? 0 : payload.length;
  const lfhUncomp = useDataDescriptor ? 0 : rawData.length;

  const lfh = Buffer.alloc(30 + nameBuf.length);
  lfh.writeUInt32LE(0x04034b50, 0);
  lfh.writeUInt16LE(20, 4);
  lfh.writeUInt16LE(flags, 6);
  lfh.writeUInt16LE(method, 8);
  lfh.writeUInt32LE(crc, 14);
  lfh.writeUInt32LE(lfhSize, 18);
  lfh.writeUInt32LE(lfhUncomp, 22);
  lfh.writeUInt16LE(nameBuf.length, 26);
  lfh.writeUInt16LE(0, 28);
  nameBuf.copy(lfh, 30);

  const dd = useDataDescriptor ? Buffer.alloc(16) : Buffer.alloc(0);
  if (useDataDescriptor) {
    dd.writeUInt32LE(0x08074b50, 0);
    dd.writeUInt32LE(crc, 4);
    dd.writeUInt32LE(payload.length, 8);
    dd.writeUInt32LE(rawData.length, 12);
  }

  const cdOffset = lfh.length + payload.length + dd.length;
  const cdfh = Buffer.alloc(46 + nameBuf.length);
  cdfh.writeUInt32LE(0x02014b50, 0);
  cdfh.writeUInt16LE(20, 4);
  cdfh.writeUInt16LE(20, 6);
  cdfh.writeUInt16LE(flags, 8);
  cdfh.writeUInt16LE(method, 10);
  cdfh.writeUInt32LE(crc, 16);
  cdfh.writeUInt32LE(payload.length, 20);
  cdfh.writeUInt32LE(rawData.length, 24);
  cdfh.writeUInt16LE(nameBuf.length, 28);
  cdfh.writeUInt16LE(0, 30);
  cdfh.writeUInt16LE(0, 32);
  cdfh.writeUInt32LE(0, 42); // relative offset of LFH
  nameBuf.copy(cdfh, 46);

  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(1, 8);
  eocd.writeUInt16LE(1, 10);
  eocd.writeUInt32LE(cdfh.length, 12);
  eocd.writeUInt32LE(cdOffset, 16);
  eocd.writeUInt16LE(0, 20);

  return Buffer.concat([lfh, payload, dd, cdfh, eocd]);
}

function makeMockRes() {
  const res = {
    statusCode: 200,
    headers: {},
    body: null,
    setHeader(k, v) { this.headers[k] = v; },
    status(code) { this.statusCode = code; return this; },
    json(data) { this.body = data; return this; },
    end() { return this; },
  };
  return res;
}

test("extractJsonFromZipBuffer handles deflated & stored ZIPs with Data Descriptors", () => {
  const sample = { status: "failed", upload_blocked: true, summary: "Missing Oct 12" };
  const deflatedZip = buildSingleFileZip("_validation_report.json", JSON.stringify(sample), { method: 8, useDataDescriptor: true });
  const storedZip = buildSingleFileZip("_validation_report.json", JSON.stringify(sample), { method: 0, useDataDescriptor: false });

  assert.deepEqual(statusHandler.extractJsonFromZipBuffer(deflatedZip), sample);
  assert.deepEqual(statusHandler.extractJsonFromZipBuffer(storedZip), sample);
  assert.equal(statusHandler.extractJsonFromZipBuffer(Buffer.from("not-a-zip")), null);
});

test("applyValidationReportToJobState marks upload blocked and propagates hard/soft findings", () => {
  const baseJob = {
    status: "completed",
    active_node: "destination_upload",
    completed_nodes: ["skidata_auth", "scraper_engine", "table_parking", "destination_upload"],
    tables_status: {
      "Revenue in Depth": "done",
      "Access in Depth": "done",
      "System Event in Depth": "done",
      "Parking Transactions": "done",
    },
    logs: [],
    error: null,
  };

  const blocked = statusHandler.applyValidationReportToJobState(structuredClone(baseJob), {
    status: "failed",
    upload_blocked: true,
    validation_enabled: true,
    summary: "Blocked: Missing 1 day",
    tables: {
      "Parking Transactions": { status: "partial", rows: 1000, message: "Partial data (29/30 days)" },
    },
    hard_failures: [{ code: "PARTIAL_DATA_MISSING_DAYS", message: "Missing 2026-10-12" }],
    soft_warnings: [],
  });

  assert.equal(blocked.status, "failed");
  assert.equal(blocked.active_node, "validation_gate");
  assert.equal(blocked.tables_status["Parking Transactions"], "partial");
  assert.ok(blocked.logs.some(l => l.includes("[HARD FAIL] [PARTIAL_DATA_MISSING_DAYS]")));

  const warned = statusHandler.applyValidationReportToJobState(structuredClone(baseJob), {
    status: "warning",
    upload_blocked: false,
    validation_enabled: true,
    summary: "1 soft warning",
    tables: {},
    hard_failures: [],
    soft_warnings: [{ code: "PORTAL_ZERO_DATA_DAY", message: "2026-10-04 returned 0 rows in Qlik" }],
  });

  assert.equal(warned.status, "completed");
  assert.ok(warned.logs.some(l => l.includes("[SOFT WARN] [PORTAL_ZERO_DATA_DAY]")));
});

test("api/execute forwards validate flag to GitHub dispatch and enforces OPA", async () => {
  const prevFetch = globalThis.fetch;
  const prevToken = process.env.GITHUB_TOKEN;
  let capturedBody = null;

  process.env.GITHUB_TOKEN = "test-gh-token";
  globalThis.fetch = async (_url, init) => {
    capturedBody = JSON.parse(init.body);
    return { ok: true, status: 204, text: async () => "" };
  };

  try {
    const resBypass = makeMockRes();
    await executeHandler({
      method: "POST",
      body: {
        authenticated: true,
        mode: "single_date",
        target_date: "2026-10-05",
        target_folder: "Validated_Docs/old",
        validate: false,
      },
    }, resBypass);

    assert.equal(resBypass.statusCode, 200);
    assert.equal(capturedBody.inputs.validate, "false");

    const resOpaDeny = makeMockRes();
    await executeHandler({
      method: "POST",
      body: {
        authenticated: true,
        mode: "single_date",
        target_date: "2026-10-05",
        target_folder: "Skidata_Daily_Files",
      },
    }, resOpaDeny);

    assert.equal(resOpaDeny.statusCode, 403);
  } finally {
    globalThis.fetch = prevFetch;
    if (prevToken === undefined) delete process.env.GITHUB_TOKEN;
    else process.env.GITHUB_TOKEN = prevToken;
  }
});
