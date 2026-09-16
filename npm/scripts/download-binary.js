#!/usr/bin/env node
// Postinstall: fetch the prebuilt `loom` binary for this platform from the
// public release, verify its checksum, and drop it in ../vendor/loom.
"use strict";

const fs = require("fs");
const path = require("path");
const https = require("https");
const crypto = require("crypto");

const REPO = process.env.LOOM_CLI_REPO || "ZKAI-Network/loom-cli";
const version = require("../package.json").version;
const tag = process.env.LOOM_VERSION || `v${version}`;

function assetName() {
  const osTag = { darwin: "darwin", linux: "linux" }[process.platform];
  const archTag = { x64: "x86_64", arm64: "arm64" }[process.arch];
  if (!osTag || !archTag) {
    console.error(
      `[loom] Unsupported platform ${process.platform}/${process.arch}. ` +
        `Mac and Linux (x86_64/arm64) are supported.`
    );
    process.exit(1);
  }
  return `loom-${osTag}-${archTag}`;
}

// GET a URL to a Buffer, following redirects (GitHub release assets 302).
function get(url) {
  return new Promise((resolve, reject) => {
    https
      .get(url, { headers: { "User-Agent": "loom-npm-installer" } }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume();
          resolve(get(res.headers.location));
          return;
        }
        if (res.statusCode !== 200) {
          reject(new Error(`GET ${url} → HTTP ${res.statusCode}`));
          res.resume();
          return;
        }
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => resolve(Buffer.concat(chunks)));
      })
      .on("error", reject);
  });
}

async function main() {
  const asset = assetName();
  const base = `https://github.com/${REPO}/releases/download/${tag}`;
  const vendorDir = path.join(__dirname, "..", "vendor");
  const dest = path.join(vendorDir, "loom");

  try {
    console.log(`[loom] Downloading ${asset} (${tag})…`);
    const [bin, sums] = await Promise.all([
      get(`${base}/${asset}`),
      get(`${base}/SHA256SUMS`),
    ]);

    const line = sums
      .toString("utf8")
      .split("\n")
      .find((l) => l.trim().endsWith(asset));
    if (!line) throw new Error(`no checksum for ${asset} in SHA256SUMS`);
    const want = line.trim().split(/\s+/)[0];
    const got = crypto.createHash("sha256").update(bin).digest("hex");
    if (want !== got) throw new Error(`checksum mismatch (expected ${want}, got ${got})`);

    fs.mkdirSync(vendorDir, { recursive: true });
    fs.writeFileSync(dest, bin, { mode: 0o755 });
    console.log(`[loom] Installed → ${dest}`);
  } catch (err) {
    console.error(`[loom] Install failed: ${err.message}`);
    console.error(`[loom] You can retry, or install via: curl -fsSL https://raw.githubusercontent.com/ZKAI-Network/loom-cli/main/install.sh | sh`);
    process.exit(1);
  }
}

main();
