"""Offline, non-root startup validation of the actual frontend release image."""

import argparse
import json
import re
import subprocess
import uuid
from pathlib import Path

from scripts.release import command, fingerprint, require, save


def smoke(work, image):
    source = json.loads((work / "source.json").read_text())
    context = json.loads((work / "context.json").read_text())
    modes = json.loads((work / "context-modes.json").read_text())
    metadata = json.loads(command(["docker", "image", "inspect", image]))[0]
    require(metadata["Config"]["User"] == "nextjs", "Final image must use nextjs")
    name = "docintel-smoke-" + uuid.uuid4().hex
    settings = {"PORT": "3000", "HOSTNAME": "0.0.0.0", "AUTH_URL": "http://localhost:3000", "AUTH_SECRET": "synthetic-startup-only-not-a-production-credential", "DOCINTEL_BATCH_DEV": "false", "DOCINTEL_BATCH_LIVE_ENABLED": "false", "AUTH_MICROSOFT_ENTRA_ID_ID": "00000000-0000-0000-0000-000000000001", "AUTH_MICROSOFT_ENTRA_ID_SECRET": "synthetic-unused-offline", "AUTH_MICROSOFT_ENTRA_ID_TENANT_ID": "https://login.invalid/synthetic/v2.0"}
    arguments = ["docker", "run", "--detach", "--network", "none", "--memory", "2g", "--cpus", "1", "--name", name]
    for key, value in settings.items():
        arguments.extend(["--env", key + "=" + value])
    try:
        command([*arguments, image])
        probe = r'''
const fs = require('node:fs');
const assert = require('node:assert/strict');
assert.equal(process.getuid(), 1001);
assert.equal(process.getgid(), 1001);
function readable(path) {
  const info = fs.statSync(path);
  fs.accessSync(path, fs.constants.R_OK | (info.isDirectory() ? fs.constants.X_OK : 0));
  if (info.isDirectory()) for (const entry of fs.readdirSync(path)) readable(path + '/' + entry);
}
readable('/app/public'); readable('/app/node_modules');
readable('/app/.next'); readable('/app/server.js'); readable('/app/next.config.ts');
assert(fs.existsSync('/app/public/mock-gens'));
for (const path of ['/app/public', '/app/public/mock-gens', '/app/server.js', '/app/node_modules', '/app/next.config.ts']) {
  assert.throws(() => fs.accessSync(path, fs.constants.W_OK));
}
fs.writeFileSync('/app/.next/cache/synthetic-smoke', 'synthetic');
(async () => {
  const deadline = Date.now() + 60000;
  let consecutive = 0;
  while (Date.now() < deadline) {
    try {
      const response = await fetch('http://127.0.0.1:3000/', {redirect: 'manual', signal: AbortSignal.timeout(2000)});
      assert(response.status >= 200 && response.status < 400);
      consecutive++;
      if (consecutive === 5) { console.log('NONROOT_ASSETS_HTTP_STABLE_OK'); return; }
    } catch { consecutive = 0; }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  throw new Error('Server failed bounded stable HTTP check');
})().catch(() => process.exit(1));
'''
        result = subprocess.run(["docker", "exec", name, "node", "-e", probe], capture_output=True, timeout=90)
        logs = subprocess.run(["docker", "logs", name], capture_output=True, timeout=15)
        permission_failure = bool(re.search(rb"EACCES|permission denied|EPERM", logs.stdout + logs.stderr, re.I))
        diagnostic = (logs.stdout + logs.stderr + result.stderr).decode(errors="replace")
        for value in settings.values():
          if len(value) > 20:
            diagnostic = diagnostic.replace(value, "[SYNTHETIC_SETTING]")
        diagnostic = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", diagnostic)
        diagnostic = re.sub(r"[A-Za-z0-9_+/=-]{80,}", "[REDACTED_LONG_VALUE]", diagnostic)
        diagnostic = "\n".join(line for line in diagnostic.splitlines() if re.search(r"EACCES|EPERM|permission|Error|code:|path:|Ready|Starting", line, re.I))[:3000]
        running = json.loads(command(["docker", "inspect", name]))[0]["State"]["Running"]
        passed = result.returncode == 0 and b"NONROOT_ASSETS_HTTP_STABLE_OK" in result.stdout and running and not permission_failure
        report = {"revision": source["revision"], "image_id": metadata["Id"], "runtime_user": metadata["Config"]["User"], "context_sha256": fingerprint(context), "modes_sha256": fingerprint(modes), "passed": passed, "running": running, "permission_failure": permission_failure, "probe_exit_code": result.returncode, "network": "none", "authentication": "synthetic startup configuration only; real hosted authentication untested"}
        save(work / "frontend-smoke.json", report)
        save(work / "frontend-smoke-diagnostics.json", {"sanitized_startup": diagnostic})
        print(diagnostic)
        print(json.dumps(report, indent=2))
        require(passed, "Frontend runtime smoke failed; raw logs withheld")
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--image", required=True)
    options = parser.parse_args()
    smoke(options.work, options.image)