#!/usr/bin/env node
// Thin launcher: exec the prebuilt `loom` binary fetched at install time.
"use strict";

const path = require("path");
const fs = require("fs");
const { spawnSync } = require("child_process");

const bin = path.join(__dirname, "..", "vendor", "loom");

if (!fs.existsSync(bin)) {
  console.error(
    "[loom] binary not found — the install step may have failed.\n" +
      "Reinstall with `npm install -g @embed-ai/loom`, or use:\n" +
      "  curl -fsSL https://raw.githubusercontent.com/ZKAI-Network/loom-cli/main/install.sh | sh"
  );
  process.exit(1);
}

const res = spawnSync(bin, process.argv.slice(2), { stdio: "inherit" });
if (res.error) {
  console.error(`[loom] failed to launch: ${res.error.message}`);
  process.exit(1);
}
process.exit(res.status === null ? 1 : res.status);
