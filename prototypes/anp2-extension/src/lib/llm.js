/**
 * Minimal multi-provider LLM client for the connected AI. API-key mode.
 * Detects the provider from the key (or uses an explicitly chosen one) and
 * calls that provider's API directly. Used to generate task results and replies.
 *
 * The key stays on-device; each fetch goes ONLY to the provider's own API host
 * (declared in host_permissions). Nothing is sent to ANP2 servers.
 */

// Supported providers. `style` selects the request/response shape.
// host = API base; model = sensible small/fast default (user can override).
export const PROVIDERS = {
  openai:     { label: "OpenAI · GPT",           host: "https://api.openai.com/v1",                        model: "gpt-4o-mini",               style: "openai" },
  anthropic:  { label: "Anthropic · Claude",     host: "https://api.anthropic.com/v1",                     model: "claude-haiku-4-5-20251001", style: "anthropic" },
  google:     { label: "Google · Gemini",        host: "https://generativelanguage.googleapis.com/v1beta", model: "gemini-2.0-flash",          style: "google" },
  openrouter: { label: "OpenRouter · any model", host: "https://openrouter.ai/api/v1",                     model: "openai/gpt-4o-mini",        style: "openai" },
};

export const DEFAULT_MODEL = Object.fromEntries(
  Object.entries(PROVIDERS).map(([k, v]) => [k, v.model]),
);

// Best-effort detection from the key's prefix. Ambiguous `sk-` defaults to
// OpenAI; the user can always override with the provider picker.
export function detectProvider(apiKey) {
  const k = (apiKey || "").trim();
  if (k.startsWith("sk-ant-")) return "anthropic";
  if (k.startsWith("sk-or-")) return "openrouter";
  if (/^AIza[0-9A-Za-z_-]{10,}$/.test(k)) return "google";
  if (k.startsWith("sk-")) return "openai";
  return "unknown";
}

/**
 * chat(apiKey, prompt, opts) -> string. `prompt` is a single user string;
 * `opts.system` optional. `opts.provider` forces a provider; otherwise detected.
 * Throws on HTTP error. `opts.fetchImpl` injectable for tests.
 */
export async function chat(apiKey, prompt, opts = {}) {
  const provider = (opts.provider && PROVIDERS[opts.provider]) ? opts.provider : detectProvider(apiKey);
  const cfg = PROVIDERS[provider];
  if (!cfg) throw new Error("unknown provider — choose one or paste a recognized API key");
  const fetchImpl = opts.fetchImpl || fetch;
  const maxTokens = opts.maxTokens || 300;
  const model = opts.model || cfg.model;

  if (cfg.style === "anthropic") {
    const r = await fetchImpl(`${cfg.host}/messages`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
      },
      body: JSON.stringify({ model, max_tokens: maxTokens, system: opts.system, messages: [{ role: "user", content: prompt }] }),
    });
    if (!r.ok) throw new Error(`anthropic HTTP ${r.status}: ${(await r.text()).slice(0, 160)}`);
    const j = await r.json();
    return (j.content || []).map((b) => b.text || "").join("").trim();
  }

  if (cfg.style === "google") {
    const body = {
      contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: { maxOutputTokens: maxTokens },
    };
    if (opts.system) body.systemInstruction = { parts: [{ text: opts.system }] };
    const r = await fetchImpl(`${cfg.host}/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(apiKey)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`google HTTP ${r.status}: ${(await r.text()).slice(0, 160)}`);
    const j = await r.json();
    return (j.candidates?.[0]?.content?.parts || []).map((p) => p.text || "").join("").trim();
  }

  // openai-compatible (OpenAI, OpenRouter, …)
  const msgs = [];
  if (opts.system) msgs.push({ role: "system", content: opts.system });
  msgs.push({ role: "user", content: prompt });
  const headers = { "content-type": "application/json", authorization: `Bearer ${apiKey}` };
  if (provider === "openrouter") { headers["HTTP-Referer"] = "https://anp2.com"; headers["X-Title"] = "ANP2"; }
  const r = await fetchImpl(`${cfg.host}/chat/completions`, {
    method: "POST", headers,
    body: JSON.stringify({ model, max_tokens: maxTokens, messages: msgs }),
  });
  if (!r.ok) throw new Error(`${provider} HTTP ${r.status}: ${(await r.text()).slice(0, 160)}`);
  const j = await r.json();
  return (j.choices?.[0]?.message?.content || "").trim();
}
