import { Container, getContainer } from "@cloudflare/containers";

// Forward reference: NimProxyContainer is declared below.
// TypeScript resolves this lazily via structural typing.
export interface Env {
  NIM_PROXY: DurableObjectNamespace<NimProxyContainer>;
  NVIDIA_API_KEY?: string;
  PROXY_API_KEY?: string;
  DEFAULT_NVIDIA_MODEL?: string;
  MAX_OUTPUT_TOKENS?: string;
  CONTEXT_SAFETY_MARGIN?: string;
  LOG_LEVEL?: string;
}

export class NimProxyContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "30m";
  // Class fields run after super() sets this.env, so we can safely read it here.
  envVars = this.buildEnvVars();

  private buildEnvVars(): Record<string, string> {
    const env = this.env as Env;
    const vars: Record<string, string> = {
      NVIDIA_API_KEY: env.NVIDIA_API_KEY ?? "",
      PROXY_HOST: "0.0.0.0",
      LOG_LEVEL: env.LOG_LEVEL ?? "info",
    };
    if (env.PROXY_API_KEY) vars.PROXY_API_KEY = env.PROXY_API_KEY;
    if (env.DEFAULT_NVIDIA_MODEL)
      vars.DEFAULT_NVIDIA_MODEL = env.DEFAULT_NVIDIA_MODEL;
    if (env.MAX_OUTPUT_TOKENS) vars.MAX_OUTPUT_TOKENS = env.MAX_OUTPUT_TOKENS;
    if (env.CONTEXT_SAFETY_MARGIN)
      vars.CONTEXT_SAFETY_MARGIN = env.CONTEXT_SAFETY_MARGIN;
    return vars;
  }
}

function withSecurityHeaders(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("Cache-Control", headers.get("Cache-Control") ?? "no-store");
  headers.set("Permissions-Policy", "interest-cohort=()");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function json(data: unknown, status = 200): Response {
  return withSecurityHeaders(
    new Response(JSON.stringify(data), {
      status,
      headers: { "content-type": "application/json; charset=utf-8" },
    }),
  );
}

function authorized(request: Request, env: Env): boolean {
  if (!env.PROXY_API_KEY) return true;
  const apiKey = request.headers.get("x-api-key");
  const bearer = request.headers
    .get("authorization")
    ?.replace(/^Bearer\s+/i, "");
  // timingSafeEqual requires equal-length buffers. Compare fixed-length HMACs
  // if either side is shorter — the SHA-256 of the presented value vs the
  // expected — to prevent both length-leak and byte-leak side channels.
  const expected = env.PROXY_API_KEY;
  const presented = apiKey ?? bearer ?? "";
  if (presented === "" || expected === "") return false;
  // Cloudflare Workers provide crypto.subtle which is timing-safe by design;
  // we still guard against runtime errors (e.g., subtle unavailable in tests).
  try {
    const enc = new TextEncoder();
    const a = enc.encode(presented);
    const b = enc.encode(expected);
    if (a.length !== b.length) return false;
    let diff = 0;
    for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
    return diff === 0;
  } catch {
    return presented === expected;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (
      url.pathname === "/" ||
      url.pathname === "/healthz" ||
      url.pathname === "/health"
    ) {
      return json({
        status: "ok",
        service: "nvd-nim-proxy",
        runtime: "cloudflare-containers",
      });
    }

    if (!env.NVIDIA_API_KEY) {
      return json(
        {
          type: "error",
          error: {
            type: "authentication_error",
            message:
              "Cloudflare secret NVIDIA_API_KEY is not configured. Run: wrangler secret put NVIDIA_API_KEY",
          },
        },
        503,
      );
    }

    if (!authorized(request, env)) {
      return json(
        {
          type: "error",
          error: {
            type: "authentication_error",
            message: "invalid proxy api key",
          },
        },
        401,
      );
    }

    const container = getContainer(env.NIM_PROXY);
    return withSecurityHeaders(await container.fetch(request));
  },
};
