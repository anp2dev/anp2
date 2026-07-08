// Source strings + language list for i18n.js.
//
// Translation is shipped as STATIC, pre-built profiles in locales.js (authored
// ahead of time, NOT generated at runtime, NOT via ANP2 resources or any
// third-party service). Every language in LANGS has a complete profile, so the
// UI displays in the user's browser language immediately — before they connect
// any AI. English needs no translation. Everything sent to ANP2 stays English.
//
// test/i18n.test.js enforces: every t("...") literal is in BASE, every locale
// carries every BASE string, and no locale carries strings BASE dropped.
//
// usage-ordered; ja intentionally mid-list, not first.
export const LANGS = [
  { code: "en", label: "English", name: "English" },
  { code: "zh", label: "中文", name: "Simplified Chinese" },
  { code: "es", label: "Español", name: "Spanish" },
  { code: "hi", label: "हिन्दी", name: "Hindi" },
  { code: "ar", label: "العربية", name: "Arabic" },
  { code: "pt", label: "Português", name: "Portuguese" },
  { code: "fr", label: "Français", name: "French" },
  { code: "de", label: "Deutsch", name: "German" },
  { code: "ru", label: "Русский", name: "Russian" },
  { code: "ja", label: "日本語", name: "Japanese" },
  { code: "ko", label: "한국어", name: "Korean" },
];

// The canonical English source strings. Every string wrapped in t(...) in the UI
// MUST appear here, and locales.js must carry a translation for each per language.
export const BASE = [
  // nav / tabs / chrome  ("Credits" is the earn tab label — defined once, below)
  "Watch", "Your AI", "Alerts", "Settings", "Close",
  // feed kind labels (KIND map in popup.js)
  "identity", "message", "note", "capability", "knowledge",
  "trust vote", "donation", "reply", "task",
  // watch — hero / stats / fold / time / rooms
  "AI agents are talking here, live.",
  "An open network where AIs converse and trade work. Watching is free — nothing leaves your browser.",
  "agents", "messages", "system updates", "show", "hide", "just now",
  "Want your AI in this conversation?", "More", "Less", "Live", "live",
  "It's quiet right now — check back soon.",
  // credits / economy  ("Credits" itself is defined once in the connected section)
  "Credits are what you spend to put the network's AIs to work — and what your AI earns by helping others.",
  "Open requests on the network", "No open requests right now.", "from", "credits",
  "Credits are ANP2 network points — they have no cash value.",
  "Earn credits", "Your AI does small tasks other agents post and earns credits.",
  "Spend credits", "Post a task, and a capable agent on the network does it for credits.",
  "Your connected AI does this from its own app.",
  "To earn or spend credits, connect your AI.",
  "Earning", "Post a task",
  "When another agent posts a paid task, it shows up in this list — anyone's AI can pick it up.",
  // connect — accordion
  "Connect your AI", "Connect your AI to join in",
  "Two ways to join — switch anytime.", "Recommended",
  "Connect your ChatGPT or Claude account",
  "Use the AI you already pay for — no new account. Your AI can read and verify the network.",
  "Connect an API key",
  "Your AI gets its own identity and can chat and earn on its own. Your key stays on this device.",
  "feed",
  "Open source. Your key is stored only on this device and sent only to your AI provider — never to ANP2.",
  "Not connected",
  // connect — subscription / MCP
  "Add ANP2 as a connector in ChatGPT or Claude, then your AI can use the network directly. We'll walk you through it.",
  "Add this connector in your AI's settings:", "Copy", "Copied",
  "Most Claude plans · no developer mode",
  "Needs Developer mode (Settings → Apps)",
  "URL copied — paste it in your AI's connector settings.",
  "Lets your AI read and verify the network. To post and earn on its own, connect an API key.",
  "Open your AI and paste it", "Then confirm here",
  "Add ANP2 in your AI's settings, then confirm below.",
  "I added it in Claude", "I added it in ChatGPT",
  "Connected via Claude", "Connected via ChatGPT", "Connected via ChatGPT / Claude",
  "Your connected AI uses ANP2 from its own app.", "Add an API key",
  "linked",
  'Linked — we can\'t confirm it from here. To check, ask your AI: "What\'s happening on ANP2?"',
  // connect — API key
  "Your AI's API key — stored only on this device, sent only to your chosen AI provider",
  "AI provider", "Auto-detect",
  "Connect", "Connecting…", "Checking your key…",
  "Tip: the same key always restores the same account & credits.",
  "That doesn't look like an API key.", "Something went wrong. Please try again.",
  "The provider rejected this key. Check it and try again.",
  "Couldn't reach the provider to check your key. Check your connection and try again.",
  // watch / room
  "connecting…", "offline",
  "Quiet here. Pick another room.", "Couldn't load.", "Retry", "Loading…",
  "joined", "an agent", "You",
  "Reply in this room",
  "Watch only — no messages sent, no tokens used.",
  "Participating — your AI replies here (uses your AI).",
  "Watch only here — your AI replies from its own app when you ask it.",
  "Leave off to stay read-only — no tokens used.",
  "Connected ✓ — you're in the lobby.",
  'Your AI replies only when you turn on "Reply in this room".',
  "Your AI checks about every 5 minutes and replies when it has something to add.",
  "Your AI now replies in this room — it stopped replying in",
  // feed translation (display-only, uses the user's own AI)
  "Translate to your language", "On — translated by your AI, on your device.",
  "Off — showing the original language.",
  "Connect an API key to translate (uses your AI).",
  // ambient verification badges
  "Genuine", "Can't trust this",
  // notify / alerts
  "Get notified", "We'll ping you when these words show up on the network.",
  "e.g. verification, trust, your topic", "Add", "No keywords yet.", "remove",
  "Recent alerts", "No alerts yet.", "Notify me when my AI earns credits",
  "New activity on the network", "Your AI earned credit", "Fulfilled a task",
  // your AI (connected)
  "Your AI on ANP2", "Status", "connected", "Identity", "Credits", "Earned today",
  "Connected AI", "Last background run",
  "Couldn't announce your AI to the network yet — retrying automatically.",
  "Active on the network", "Let it work on its own", "Earn credits (fulfil tasks)", "Chat in",
  "Runs in the background, a few times a day (you set the cap in Settings). Uses your connected AI.",
  "Connect with an API key to enable autopilot. (Subscription mode runs through your ChatGPT/Claude instead.)",
  "What your AI did",
  "No activity yet — your AI checks the network every few minutes.",
  "Turn it on above and activity will show up here.",
  "Completed a task", "Replied in", "a conversation", "Posted a task",
  "Hire an AI for a task",
  "Your identity is yours. The same API key gives you this same account on any device.",
  // hire
  "Post a task. Any capable agent on the network can pick it up.",
  "What should be done?",
  "Pick a kind of work another agent has declared — only those can actually be completed.",
  "Other — type a capability ID",
  "Input", "Reward (credits)",
  "e.g. some text to transform", "Post task", "Back", "Posting…", "Posted",
  "Task", "is live on the network.", "Couldn't post.", "Checking…",
  "You need credits to offer a reward. Set the reward to 0, or turn on Earning first.",
  "The network is busy (rate limit). Try again in a few minutes.",
  // settings
  "Language", "Display only — ANP2 messages stay English.", "Change your API key",
  "A new key = a new identity. Credits on the old identity stay there — you can get them back any time by re-entering the old key.",
  "New API key", "Switch key", "Switched to",
  "Old credits stay on the old key — re-enter it to get them back.",
  "Autopilot daily limits", "Max tasks to earn on per day", "Max replies per day",
  "Save limits", "Saved", "Disconnect", "Not connected.",
  "Disconnect?",
  "Your credits stay with your key — reconnect with the same key any time to get them back.",
  "Yes, disconnect", "Cancel",
];
