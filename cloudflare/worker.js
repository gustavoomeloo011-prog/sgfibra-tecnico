const FAILURE_STATUS = 502;

function targetRequest(request, origin, timeoutMs) {
  const incoming = new URL(request.url);
  const target = new URL(incoming.pathname + incoming.search, origin);
  const headers = new Headers(request.headers);
  headers.set("ngrok-skip-browser-warning", "1");
  headers.set("x-ebf-gateway", "cloudflare");
  headers.delete("host");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort("origin timeout"), timeoutMs);
  const proxied = new Request(target, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD"
      ? undefined
      : request.body,
    redirect: "manual",
    signal: controller.signal,
  });
  return { proxied, timer };
}

async function attempt(request, origin, timeoutMs) {
  const { proxied, timer } = targetRequest(request, origin, timeoutMs);
  try {
    return await fetch(proxied);
  } finally {
    clearTimeout(timer);
  }
}

export default {
  async fetch(request, env) {
    if (env.SYSTEM_DISABLED !== "0") {
      return Response.json(
        {
          error: "EBF Técnico temporariamente desativado para diagnóstico.",
          maintenance: true,
        },
        {
          status: 503,
          headers: {
            "cache-control": "no-store",
            "retry-after": "3600",
            "x-ebf-origin": "maintenance",
          },
        },
      );
    }
    const requestForFallback = request.clone();
    try {
      const primary = await attempt(
        request,
        env.PRIMARY_ORIGIN,
        Number(env.PRIMARY_TIMEOUT_MS || 1800),
      );
      const ngrokUnavailable = primary.headers.has("ngrok-error-code");
      if (primary.status < 500 && !ngrokUnavailable) {
        const response = new Response(primary.body, primary);
        response.headers.set("x-ebf-origin", "pc");
        return response;
      }
    } catch {
      // Computador desligado ou túnel indisponível: usa o Render.
    }

    try {
      const fallback = await attempt(
        requestForFallback,
        env.FALLBACK_ORIGIN,
        Number(env.FALLBACK_TIMEOUT_MS || 70000),
      );
      const response = new Response(fallback.body, fallback);
      response.headers.set("x-ebf-origin", "render");
      return response;
    } catch {
      return Response.json(
        { error: "Os dois servidores estão temporariamente indisponíveis." },
        { status: FAILURE_STATUS },
      );
    }
  },
};
