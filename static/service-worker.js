const CACHE = "zenstats-shell-v2";

async function saveScreen(response) {
  if (!response || !response.ok) return false;

  const type = response.headers.get("content-type") || "";

  if (!type.includes("text/html")) return false;

  const html = await response.clone().text();

  // Save the actual app, not Render's startup screen.
  if (
    !html.includes("<title>ZenStats</title>") ||
    !html.includes('id="content"')
  ) {
    return false;
  }

  const cache = await caches.open(CACHE);

  await cache.put("/", response.clone());

  return true;
}

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const response = await fetch("/", {
      cache: "no-store"
    });

    if (!(await saveScreen(response))) {
      throw new Error(
        "ZenStats screen could not be saved yet."
      );
    }

    await self.skipWaiting();
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const names = await caches.keys();

    await Promise.all(
      names.filter(name =>
        (
          name.startsWith("nba-tracker-shell-") ||
          name.startsWith("zenstats-shell-")
        ) &&
        name !== CACHE
      ).map(name => caches.delete(name))
    );

    await self.clients.claim();
  })());
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  if (
    request.method !== "GET" ||
    url.origin !== self.location.origin ||
    request.mode !== "navigate" ||
    url.pathname !== "/"
  ) {
    return;
  }

  const refresh = fetch(request, {
    cache: "no-store"
  }).then(async response => {
    await saveScreen(response);
    return response;
  });

  event.waitUntil(
    refresh
      .then(() => undefined)
      .catch(() => undefined)
  );

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const saved = await cache.match("/");

    // Open the saved screen without waiting for Render.
    if (saved) return saved;

    try {
      return await refresh;
    } catch (error) {
      return new Response(
        `<!doctype html>
        <html lang="en">
        <head>
          <meta name="viewport" content="width=device-width">
          <title>ZenStats</title>
        </head>
        <body style="
          background:#000;
          color:#f4f5fb;
          font:16px system-ui;
          padding:24px;
        ">
          <h1>ZenStats</h1>
          <p>Reconnect to load the app.</p>
          <button onclick="location.reload()">Retry</button>
        </body>
        </html>`,
        {
          status: 503,
          headers: {
            "Content-Type": "text/html; charset=utf-8"
          }
        }
      );
    }
  })());
});
