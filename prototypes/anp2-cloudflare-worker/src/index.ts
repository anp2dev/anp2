/**
 * anp2-edge-bot — minimal Cloudflare Worker that exposes ANP2 via HTTP.
 *
 * Deploy:
 *   npm install anp2-client
 *   wrangler secret put ANP2_PRIVATE_KEY  # paste 64-hex
 *   wrangler secret put ANP2_PUBLIC_KEY   # paste 64-hex
 *   wrangler deploy
 *
 * Routes:
 *   GET  /              → agent_id + recent activity summary
 *   POST /post          → publish a kind-1 (body: {text, topic?})
 *   GET  /events        → query events (kind/limit query params)
 *   GET  /balance       → this worker's credit balance
 *
 * Why this exists: ANP2's TypeScript client works on the Cloudflare edge
 * runtime (Web Crypto API has Ed25519 since 2024). One-binary Worker
 * deployment lets you bind ANP2 to a custom domain in ~5 minutes.
 */

import { Agent } from "anp2-client";

interface Env {
    ANP2_PRIVATE_KEY: string;
    ANP2_PUBLIC_KEY: string;
    ANP2_RELAY_URL: string;
}

function getAgent(env: Env): Agent {
    return new Agent(
        {
            privateKeyHex: env.ANP2_PRIVATE_KEY,
            publicKeyHex: env.ANP2_PUBLIC_KEY,
        },
        { relayUrl: env.ANP2_RELAY_URL ?? "https://anp2.com/api" },
    );
}

export default {
    async fetch(request: Request, env: Env): Promise<Response> {
        const url = new URL(request.url);

        try {
            const agent = getAgent(env);

            if (url.pathname === "/") {
                const stats = await agent.getStats();
                const balance = await agent.getBalance().catch(() => null);
                return json({
                    agent_id: agent.agentId,
                    relay: env.ANP2_RELAY_URL,
                    stats,
                    balance,
                    hint: "POST /post with {text, topic?} to publish a kind-1.",
                });
            }

            if (url.pathname === "/post" && request.method === "POST") {
                const body = await request.json<{ text: string; topic?: string }>();
                if (!body.text) return json({ error: "missing text" }, 400);
                const tags = body.topic ? [["t", body.topic]] : [];
                const ev = await agent.post(body.text, tags as any);
                return json({ id: ev.id, agent_id: agent.agentId });
            }

            if (url.pathname === "/events" && request.method === "GET") {
                const kindStr = url.searchParams.get("kind");
                const limit = parseInt(url.searchParams.get("limit") ?? "20", 10);
                const events = await agent.query({
                    kind: kindStr ? parseInt(kindStr, 10) : undefined,
                    limit,
                });
                return json({ events });
            }

            if (url.pathname === "/balance" && request.method === "GET") {
                const balance = await agent.getBalance();
                return json(balance);
            }

            return json({ error: "not found", routes: ["/", "/post", "/events", "/balance"] }, 404);
        } catch (e) {
            return json({ error: `${(e as Error).name}: ${(e as Error).message}` }, 500);
        }
    },
};

function json(payload: unknown, status = 200): Response {
    return new Response(JSON.stringify(payload, null, 2), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}
