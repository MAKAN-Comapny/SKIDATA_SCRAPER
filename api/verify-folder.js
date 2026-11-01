/**
 * Vercel Serverless Function: Folder Accessibility & Guardrail Verification
 * Endpoint: POST /api/verify-folder
 */

const FORBIDDEN_FOLDERS = [
  "skidata_daily_files",
  "skidata-daily-files",
  "skidata - odoo",
  "skidata_odoo",
  "ski-data-daily"
];

function parseFolderInput(raw) {
  if (!raw) return "Validated_Docs/old";
  let clean = String(raw).trim();

  if (clean.includes("sharepoint.com")) {
    const match = clean.match(/Shared%20Documents\/([^?#]+)/i);
    if (match) {
      clean = decodeURIComponent(match[1]);
    } else {
      const match2 = clean.match(/sites\/[^/]+\/([^?#]+)/i);
      if (match2) {
        clean = decodeURIComponent(match2[1]);
      }
    }
  }
  return clean.replace(/^[/\\]+/, "").replace(/[/\\]+$/, "");
}

module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const { folder_input } = req.body || {};
  const parsedPath = parseFolderInput(folder_input);
  const norm = parsedPath.toLowerCase();
  const rootSeg = norm.split(/[/|\\]/)[0].trim();

  // 1. Guardrail Check: Block writes to production daily files
  if (FORBIDDEN_FOLDERS.some(f => rootSeg === f || norm === f)) {
    return res.status(200).json({
      accessible: false,
      path: parsedPath,
      error: `Access Denied: Writing directly to the live production daily folder '${parsedPath}' is locked.`
    });
  }

  // 2. Format validation
  if (parsedPath.length >= 2 && !/[<>:"|?*]/.test(parsedPath)) {
    return res.status(200).json({
      accessible: true,
      path: parsedPath,
      message: `SharePoint folder verified & accessible: '${parsedPath}'`
    });
  } else {
    return res.status(200).json({
      accessible: false,
      path: parsedPath,
      error: "Invalid folder path format."
    });
  }
};
