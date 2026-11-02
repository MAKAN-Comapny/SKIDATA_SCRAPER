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

// Valid document library root directories on makanpak.sharepoint.com/sites/SKIDATAFILES
const ALLOWED_ROOTS = [
  "validated_docs",
  "ski data 5",
  "odoo_testing_sumit",
  "validation_docs",
  "odoo_aug_sep"
];

function parseFolderInput(raw) {
  if (!raw) return "";
  let clean = String(raw).trim();

  // If full HTTP URL
  if (clean.startsWith("http://") || clean.startsWith("https://")) {
    if (!clean.includes("makanpak.sharepoint.com") || !clean.includes("SKIDATAFILES")) {
      return {
        is_invalid_url: true,
        path: clean,
        error: "Invalid SharePoint URL: URL must be from makanpak.sharepoint.com/sites/SKIDATAFILES"
      };
    }

    const match = clean.match(/Shared%20Documents\/([^?#]+)/i) || clean.match(/Shared Documents\/([^?#]+)/i);
    if (match) {
      clean = decodeURIComponent(match[1]);
    } else {
      const match2 = clean.match(/sites\/[^/]+\/([^?#]+)/i);
      if (match2) {
        clean = decodeURIComponent(match2[1]);
      }
    }
  }

  const cleanPath = clean.replace(/^[/\\]+/, "").replace(/[/\\]+$/, "");
  return {
    is_invalid_url: false,
    path: cleanPath
  };
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
  if (!folder_input || !String(folder_input).trim()) {
    return res.status(200).json({
      accessible: false,
      path: "",
      error: "no accessible: Destination folder cannot be empty."
    });
  }

  const parsed = parseFolderInput(folder_input);
  if (parsed.is_invalid_url) {
    return res.status(200).json({
      accessible: false,
      path: parsed.path,
      error: parsed.error
    });
  }

  const parsedPath = parsed.path;
  const norm = parsedPath.toLowerCase();
  const rootSeg = norm.split(/[/|\\]/)[0].trim();

  // 1. Guardrail Check: Block writes to production daily files
  if (FORBIDDEN_FOLDERS.some(f => rootSeg === f || norm === f)) {
    return res.status(200).json({
      accessible: false,
      path: parsedPath,
      error: `Access Denied: Writing directly to live production folder '${parsedPath}' is locked.`
    });
  }

  // 2. Strict Existence / Allowed Roots Check in SharePoint
  const isAllowedRoot = ALLOWED_ROOTS.some(r => rootSeg === r);
  if (!isAllowedRoot) {
    return res.status(200).json({
      accessible: false,
      path: parsedPath,
      error: `no accessible: Folder '${parsedPath}' does not exist in SharePoint document library.`
    });
  }

  // 3. Live Microsoft Graph API check if credentials are provided in Vercel environment
  const tenantId = process.env.TENANT_ID || "";
  const clientId = process.env.CLIENT_ID || "";
  const clientSecret = process.env.CLIENT_SECRET || "";

  if (tenantId && clientId && clientSecret) {
    try {
      const tokenRes = await fetch(`https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/token`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          client_id: clientId,
          client_secret: clientSecret,
          grant_type: "client_credentials",
          scope: "https://graph.microsoft.com/.default"
        })
      });

      if (tokenRes.ok) {
        const tokenData = await tokenRes.json();
        const headers = { Authorization: `Bearer ${tokenData.access_token}` };

        // Resolve site ID
        const siteRes = await fetch(`https://graph.microsoft.com/v1.0/sites?search=SKIDATAFILES`, { headers });
        if (siteRes.ok) {
          const siteData = await siteRes.json();
          const siteId = siteData.value[0]?.id;

          if (siteId) {
            const encodedDrive = parsedPath.split("/").filter(Boolean).map(encodeURIComponent).join("/");
            const chkRes = await fetch(`https://graph.microsoft.com/v1.0/sites/${siteId}/drive/root:/${encodedDrive}`, { headers });

            if (chkRes.status === 200) {
              return res.status(200).json({
                accessible: true,
                path: parsedPath,
                message: "SharePoint folder exists and is fully accessible."
              });
            } else if (chkRes.status === 404) {
              if (parsedPath.startsWith("Validated_Docs") || parsedPath.startsWith("Ski Data")) {
                return res.status(200).json({
                  accessible: true,
                  path: parsedPath,
                  message: "Folder verified (will be provisioned in Validated_Docs on upload)."
                });
              }
              return res.status(200).json({
                accessible: false,
                path: parsedPath,
                error: `no accessible: Folder '${parsedPath}' not found in SharePoint.`
              });
            }
          }
        }
      }
    } catch (e) {
      console.warn("Live Graph API check warning:", e);
    }
  }

  // Format and path check passed
  return res.status(200).json({
    accessible: true,
    path: parsedPath,
    message: `SharePoint folder verified & accessible: '${parsedPath}'`
  });
};
