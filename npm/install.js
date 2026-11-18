#!/usr/bin/env node
"use strict";

const { execSync, spawnSync } = require("child_process");
const os = require("os");

const MINIMUM_PYTHON = [3, 9];

function log(msg) {
  console.log(`[free-code] ${msg}`);
}

function warn(msg) {
  console.warn(`[free-code] WARNING: ${msg}`);
}

function error(msg) {
  console.error(`[free-code] ERROR: ${msg}`);
}

// Try running a command, return stdout or null
function tryExec(cmd) {
  try {
    return execSync(cmd, { encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }).trim();
  } catch {
    return null;
  }
}

// Find a working Python >= 3.9
function findPython() {
  const candidates = os.platform() === "win32"
    ? ["py -3", "python", "python3"]
    : ["python3", "python", "py"];

  for (const cmd of candidates) {
    const version = tryExec(`${cmd} --version`);
    if (version) {
      const match = version.match(/Python (\d+)\.(\d+)/);
      if (match) {
        const major = parseInt(match[1], 10);
        const minor = parseInt(match[2], 10);
        if (major > MINIMUM_PYTHON[0] || (major === MINIMUM_PYTHON[0] && minor >= MINIMUM_PYTHON[1])) {
          return { cmd, version: `${major}.${minor}` };
        }
      }
    }
  }
  return null;
}

// Find a working pip
function findPip(pythonCmd) {
  // Try pip as a module first (most reliable)
  if (tryExec(`${pythonCmd} -m pip --version`) !== null) {
    return { cmd: `${pythonCmd} -m pip`, type: "pip" };
  }

  const pipCandidates = os.platform() === "win32"
    ? ["pip", "pip3"]
    : ["pip3", "pip"];

  for (const cmd of pipCandidates) {
    if (tryExec(`${cmd} --version`) !== null) {
      return { cmd, type: "pip" };
    }
  }

  // Try pipx
  if (tryExec("pipx --version") !== null) {
    return { cmd: "pipx", type: "pipx" };
  }

  return null;
}

function main() {
  log("Setting up free-code...");

  // Check if free-code Python package is already installed
  const existing = tryExec("free-code --version");
  if (existing) {
    log(`free-code is already installed (${existing})`);
    return;
  }

  // Find Python
  const python = findPython();
  if (!python) {
    error("Python 3.9+ is required but not found.");
    error("Install Python from https://python.org or https://free.ai/install");
    console.error("");
    error("After installing Python, run: pip install freeai-code");
    process.exit(0); // Don't fail npm install, the bin script will show the error
  }

  log(`Found Python ${python.version} (${python.cmd})`);

  // Find pip
  const pip = findPip(python.cmd);
  if (!pip) {
    warn("pip not found. Trying to bootstrap it...");
    const bootstrap = tryExec(`${python.cmd} -m ensurepip --upgrade`);
    if (bootstrap === null) {
      error("Could not find or install pip.");
      error("Install pip manually: https://pip.pypa.io/en/stable/installation/");
      error("Then run: pip install freeai-code");
      process.exit(0);
    }
  }

  const pipInfo = pip || findPip(python.cmd);
  if (!pipInfo) {
    error("pip setup failed. Install manually: pip install freeai-code");
    process.exit(0);
  }

  log(`Installing free-code Python package via ${pipInfo.type}...`);

  let installCmd;
  if (pipInfo.type === "pipx") {
    installCmd = "pipx install freeai-code";
  } else {
    installCmd = `${pipInfo.cmd} install freeai-code`;
  }

  const result = spawnSync(installCmd, {
    shell: true,
    stdio: "inherit",
    env: { ...process.env },
  });

  if (result.status !== 0) {
    // Try with --user flag if regular install fails
    if (pipInfo.type === "pip") {
      log("Retrying with --user flag...");
      const retry = spawnSync(`${pipInfo.cmd} install --user freeai-code`, {
        shell: true,
        stdio: "inherit",
      });
      if (retry.status !== 0) {
        warn("pip install failed. You may need to run manually:");
        warn(`  ${pipInfo.cmd} install freeai-code`);
        process.exit(0);
      }
    } else {
      warn("Installation failed. Run manually: pip install freeai-code");
      process.exit(0);
    }
  }

  log("free-code installed successfully!");
  log("Run 'free-code' to start your AI coding assistant.");
}

main();
