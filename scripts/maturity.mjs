#!/usr/bin/env node
// wheel: maturity index + operational gap for a candidate repo.
// Deterministic: 5 boolean flags -> A/B/C/D. No model involved.
//
//   node scripts/maturity.mjs n8n-io/n8n kestra-io/kestra
//   node scripts/maturity.mjs --json owner/repo
//   node scripts/maturity.mjs --self-check

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CACHE = join(ROOT, "registry", "maturity.json");
const TTL_MS = 7 * 864e5;
const DAY = 864e5;

// ── pure logic (covered by --self-check) ──────────────────────

export function flags(r, now) {
  return {
    alive: now - Date.parse(r.pushed_at) < 90 * DAY,
    adopted: r.stars > 500,
    sustained: r.releases_12mo >= 2,
    safe: Boolean(r.license) && !r.archived,
    bus: r.contributors >= 3,
  };
}

export function level(f, r) {
  if (r.archived) return "D"; // donor: code survives, support does not
  const n = Object.values(f).filter(Boolean).length;
  return n === 5 ? "A" : n === 4 ? "B" : n >= 2 ? "C" : "D";
}

// Operational gap: how far the repo is from running where the user needs it.
export function opsGap(paths) {
  const has = (re) => paths.some((p) => re.test(p));
  const signals = {
    compose: has(/^(docker-)?compose\.ya?ml$/i),
    helm: has(/^(charts?|helm)$/i) || has(/^Chart\.ya?ml$/i),
    k8s: has(/^(k8s|kubernetes|manifests|deploy)$/i),
    docker: has(/^Dockerfile/i),
    pkg: has(/^(package\.json|pyproject\.toml|Cargo\.toml|go\.mod|composer\.json)$/i),
  };
  const gap = signals.compose || signals.helm ? "none" : signals.docker || signals.pkg ? "small" : "large";
  return { ...signals, gap };
}

// ── github access ─────────────────────────────────────────────

function gh(path) {
  try {
    return JSON.parse(execFileSync("gh", ["api", path], { encoding: "utf8", maxBuffer: 32 << 20 }));
  } catch (e) {
    if (e.code === "ENOENT") throw new Error("gh CLI not found — install GitHub CLI and run `gh auth login`");
    return null; // 404 / rate limit / private: treat as "no data", flags degrade honestly
  }
}

function probe(slug, now) {
  const repo = gh(`repos/${slug}`);
  if (!repo) throw new Error(`cannot read repos/${slug} (missing, private, or rate-limited)`);

  const releases = gh(`repos/${slug}/releases?per_page=100`) ?? [];
  const contributors = gh(`repos/${slug}/contributors?per_page=10`) ?? [];
  const root = gh(`repos/${slug}/contents?ref=${repo.default_branch}`) ?? [];

  const r = {
    slug,
    stars: repo.stargazers_count,
    pushed_at: repo.pushed_at,
    archived: repo.archived,
    license: repo.license?.spdx_id && repo.license.spdx_id !== "NOASSERTION" ? repo.license.spdx_id : null,
    releases_12mo: releases.filter((x) => now - Date.parse(x.published_at ?? x.created_at) < 365 * DAY).length,
    contributors: contributors.length,
  };
  const f = flags(r, now);
  return { ...r, flags: f, level: level(f, r), ops: opsGap(root.map((x) => x.name)) };
}

// ── cache ─────────────────────────────────────────────────────

const readCache = () => (existsSync(CACHE) ? JSON.parse(readFileSync(CACHE, "utf8")) : {});

function writeCache(cache) {
  mkdirSync(dirname(CACHE), { recursive: true });
  writeFileSync(CACHE, JSON.stringify(cache, null, 2) + "\n");
}

// ── self-check ────────────────────────────────────────────────

async function selfCheck() {
  const { strict: assert } = await import("node:assert");
  const now = Date.parse("2026-08-15T00:00:00Z");
  const fresh = "2026-08-01T00:00:00Z";
  const stale = "2023-01-01T00:00:00Z";

  const perfect = { pushed_at: fresh, stars: 9000, releases_12mo: 12, license: "MIT", archived: false, contributors: 40 };
  assert.equal(level(flags(perfect, now), perfect), "A");

  const noReleases = { ...perfect, releases_12mo: 0 };
  assert.equal(level(flags(noReleases, now), noReleases), "B");

  const hobby = { pushed_at: fresh, stars: 12, releases_12mo: 0, license: "MIT", archived: false, contributors: 1 };
  assert.equal(level(flags(hobby, now), hobby), "C");

  const dead = { pushed_at: stale, stars: 12, releases_12mo: 0, license: null, archived: false, contributors: 1 };
  assert.equal(level(flags(dead, now), dead), "D");

  // archived beats everything: a perfect repo nobody maintains is a donor
  const archived = { ...perfect, archived: true };
  assert.equal(level(flags(archived, now), archived), "D");
  assert.equal(flags(archived, now).safe, false);

  // no license is not "safe" even when alive and popular
  assert.equal(flags({ ...perfect, license: null }, now).safe, false);

  assert.equal(opsGap(["docker-compose.yml", "src"]).gap, "none");
  assert.equal(opsGap(["Chart.yaml"]).gap, "none");
  assert.equal(opsGap(["Dockerfile", "src"]).gap, "small");
  assert.equal(opsGap(["package.json"]).gap, "small");
  assert.equal(opsGap(["README.md", "main.c", "Makefile"]).gap, "large");

  console.log("self-check: ok");
}

// ── cli ───────────────────────────────────────────────────────

const argv = process.argv.slice(2);
if (argv.includes("--self-check")) {
  await selfCheck();
} else {
  const asJson = argv.includes("--json");
  const slugs = argv.filter((a) => !a.startsWith("--"));
  if (!slugs.length) {
    console.error("usage: maturity.mjs <owner/repo>... [--json] | --self-check");
    process.exit(2);
  }

  const now = Date.now();
  const cache = readCache();
  const out = [];

  for (const slug of slugs) {
    const hit = cache[slug];
    const entry = hit && now - Date.parse(hit.checked_at) < TTL_MS ? hit : { ...probe(slug, now), checked_at: new Date(now).toISOString() };
    cache[slug] = entry;
    out.push(entry);
  }
  writeCache(cache);

  if (asJson) {
    console.log(JSON.stringify(out, null, 2));
  } else {
    for (const e of out) {
      const on = Object.entries(e.flags).filter(([, v]) => v).map(([k]) => k).join(" ") || "—";
      console.log(`${e.level}  ${e.slug}  ★${e.stars}  deploy-gap:${e.ops.gap}  [${on}]`);
    }
  }
}
