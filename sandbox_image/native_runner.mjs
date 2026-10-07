// Trusted launcher; model commands only run inside the OS policy produced by SRT.
import { SandboxManager } from '../web/node_modules/@anthropic-ai/sandbox-runtime/dist/index.js';
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { isIP } from 'node:net';
import { canonicalizeHost } from '../web/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/parent-proxy.js';
import { isLoopbackName } from '../web/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/address.js';
import { createResolvedAddressGuard } from '../web/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/resolved-address-guard.js';

export function outboundFilter(settings) {
  const guard = createResolvedAddressGuard(settings.network);
  return async ({ host, port }) => {
    const canonical = canonicalizeHost(host);
    if (!settings.allowExternalNetwork || !canonical || isLoopbackName(canonical)) return false;
    // SRT regards a literal allowed by this callback as an explicit exception.
    // Apply the hostname address rules to literals too, including local interfaces.
    return !isIP(canonical) || guard.permits('dra-outbound.invalid', canonical, port);
  };
}

if (process.argv[2] === '--check') {
  const check = await SandboxManager.checkDependenciesAsync();
  console.log(JSON.stringify(check));
  process.exitCode = check.errors.length ? 1 : 0;
} else if (process.argv[2]) {
  const expires = Number(process.argv[3]);
  const parent = Number(process.argv[4]);
  const stop = () => process.kill(-process.pid, 'SIGKILL');
  // Same command deadline as the controller. Survives a service crash and retains
  // the inherited workspace lease until this command group is stopped.
  if (process.ppid !== parent || Date.now() >= expires) stop();
  const expiry = setTimeout(stop, Math.max(1, expires - Date.now()));
  const parentWatch = setInterval(() => { if (process.ppid !== parent) stop(); }, 100);
  expiry.unref(); parentWatch.unref();
  const settings = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const { allowExternalNetwork, ...policy } = settings;
  await SandboxManager.initialize(policy, outboundFilter(settings), false);
  try {
    const command = process.argv.slice(5).map(s => "'" + s.replaceAll("'", "'\\''") + "'").join(' ');
    const wrapped = await SandboxManager.wrapWithSandboxArgv(command, '/bin/bash');
    const child = spawn(wrapped.argv[0], wrapped.argv.slice(1), {
      env: wrapped.env, stdio: 'inherit',
    });
    process.exitCode = await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve(code ?? (signal ? 128 : 1)));
    });
  } finally {
    SandboxManager.cleanupAfterCommand();
    await SandboxManager.reset();
  }
}
