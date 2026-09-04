// Wheel catalog edge.
//
// Serves the daily catalog from Cloudflare KV so a client has an origin that does
// not depend on GitHub being reachable. Read-only by construction: there is no
// code path here that writes, and the token that publishes lives in CI, never in
// the Worker.
//
// KV is the store rather than R2 because R2 requires the account owner to enable
// it in the dashboard, while KV is available immediately. The catalog is a few
// hundred kilobytes, far under KV's 25 MB value ceiling.

const OBJECTS = {
  '/catalog.jsonl': { key: 'catalog.jsonl', type: 'application/x-ndjson' },
  '/catalog.meta.json': { key: 'catalog.meta.json', type: 'application/json' },
};

const CACHE_CONTROL = 'public, max-age=3600';

function notFound() {
  return new Response('not found\n', {
    status: 404,
    headers: { 'content-type': 'text/plain; charset=utf-8' },
  });
}

async function serveObject(env, path, request) {
  const entry = OBJECTS[path];
  if (!entry) {
    return notFound();
  }
  const stored = await env.CATALOG.getWithMetadata(entry.key, { type: 'text' });
  if (stored === null || stored.value === null) {
    return notFound();
  }
  const metadata = stored.metadata || {};
  const etag = metadata.etag ? `"${metadata.etag}"` : undefined;
  const headers = {
    'content-type': `${entry.type}; charset=utf-8`,
    'cache-control': CACHE_CONTROL,
  };
  if (etag) {
    headers.etag = etag;
  }
  if (metadata.uploaded_at) {
    headers['x-catalog-uploaded-at'] = metadata.uploaded_at;
  }
  if (etag && request.headers.get('if-none-match') === etag) {
    return new Response(null, { status: 304, headers });
  }
  if (request.method === 'HEAD') {
    return new Response(null, { status: 200, headers });
  }
  return new Response(stored.value, { status: 200, headers });
}

async function health(env) {
  const stored = await env.CATALOG.getWithMetadata('catalog.jsonl', { type: 'text' });
  if (stored === null || stored.value === null) {
    return new Response(JSON.stringify({ status: 'empty' }) + '\n', {
      status: 503,
      headers: { 'content-type': 'application/json; charset=utf-8' },
    });
  }
  const metadata = stored.metadata || {};
  const body = {
    status: 'ok',
    uploaded_at: metadata.uploaded_at || null,
    records: metadata.records || null,
    bytes: stored.value.length,
  };
  return new Response(JSON.stringify(body) + '\n', {
    status: 200,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    },
  });
}

export default {
  async fetch(request, env) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('method not allowed\n', {
        status: 405,
        headers: {
          'content-type': 'text/plain; charset=utf-8',
          allow: 'GET, HEAD',
        },
      });
    }
    const url = new URL(request.url);
    if (url.pathname === '/health') {
      return health(env);
    }
    return serveObject(env, url.pathname, request);
  },
};
