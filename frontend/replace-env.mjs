import { readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

const names = ["API_PROTOCOL", "API_HOSTNAME", "API_PORT", "STORAGE_ACCOUNT_NAME", "DEFAULT_BRAND"];

async function replace(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      await replace(path);
    } else if (entry.isFile() && entry.name.endsWith(".js")) {
      const original = await readFile(path, "utf8");
      let updated = original;
      for (const name of names) {
        updated = updated.replaceAll(`__NEXT_PUBLIC_${name}__`, process.env[`NEXT_PUBLIC_${name}`] ?? "");
      }
      if (updated !== original) await writeFile(path, updated);
    }
  }
}

await replace("/app/.next");