/**
 * Vercel Serverless Function: Authentication
 * Endpoint: POST /api/login
 */

module.exports = async function handler(req, res) {
  // Enable CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const { username, password } = req.body || {};
  const validUser = process.env.PORTAL_USER || "admin";
  const validPass = process.env.PORTAL_PASSWORD || "Makan@2026!";

  if (username === validUser && password === validPass) {
    return res.status(200).json({
      success: true,
      token: "skidata_makan_bearer_token_2026",
      user: username,
      role: "Makan Operations Administrator"
    });
  }

  return res.status(401).json({
    success: false,
    detail: "Invalid username or password."
  });
};
