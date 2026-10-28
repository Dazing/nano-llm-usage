// Disposable ESM loader for the Node probes in this directory: the app imports its own
// modules without an extension (Vite style), which Node's ESM resolver rejects.
// Used by: node --import ./probe_register.mjs probe_cadence.ts
import { existsSync } from "node:fs";

export async function resolve(specifier, context, next) {
  if (specifier.startsWith(".") && !/\.[a-z]+$/.test(specifier)) {
    const url = new URL(`${specifier}.ts`, context.parentURL);
    if (existsSync(url)) return { url: url.href, shortCircuit: true };
  }
  return next(specifier, context);
}
