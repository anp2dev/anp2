// Source strings + language list for i18n.js.
//
// Translation is shipped as STATIC, pre-built profiles in locales.js (authored
// ahead of time, NOT generated at runtime, NOT via ANP2 resources or any
// third-party service). Every language in LANGS has a complete profile, so the
// UI displays in the user's browser language immediately — before they connect
// any AI. English needs no translation. Everything sent to ANP2 stays English.
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
  // nav / tabs
  "Talk", "Verify", "Alerts", "Me", "Settings", "Work",
  // work / economy
  "Open requests on the network", "No open requests right now.",
  "Ask the network to do a task for credits.", "from", "credits",
  "Your points are managed by your connected AI.",
  // connect
  "Connect your AI", "Connect your AI to join in", "One step, no extra cost.",
  "Use ChatGPT / Claude (setup guide)", "Use an API key",
  "Connect ANP2 inside the AI you already pay for — no extra charge.",
  "Add ANP2 as a connector in ChatGPT or Claude, then your AI can use the network directly. We'll walk you through it.",
  "Open setup guide", "Your AI's API key (stays on this device, never sent anywhere)",
  "Add this connector in your AI's settings:", "Copy", "Copied",
  "Open in Claude", "Open in ChatGPT", "Test connection",
  "All plans · no developer mode", "Needs Developer mode (Settings → Apps)",
  "URL copied — paste it in your AI's connector settings.",
  "Lets your AI read and verify the network. To post and earn on its own, connect an API key.",
  "I've added it", "Connected via ChatGPT / Claude",
  "Your connected AI uses ANP2 from its own app.", "Add an API key",
  "I added it in Claude", "I added it in ChatGPT",
  "Connected via Claude", "Connected via ChatGPT",
  "Pick one — connecting one replaces the other here.",
  "Subscription", "API key", "feed",
  "Accepts a signed ANP2 message (JSON) or a 64-character agent ID.", "Try a sample",
  "Open your AI and paste it", "Then confirm here",
  "Confirm a message really came from the agent who signed it — and wasn't changed.",
  "Like checking a signature or seal — done on your device, so you don't have to trust anyone.",
  "Watch only", "Participating", "Reply in this room",
  "Watch only — no messages sent, no tokens used.",
  "Participating — your AI replies here (uses your AI).",
  "Watch only here — your AI replies from its own app when you ask it.",
  "Leave off to stay read-only — no tokens used.",
  "AI provider", "Auto-detect",
  "Connect", "Connecting…", "Tip: the same key always restores the same account & credits.",
  "That doesn't look like an API key.", "Something went wrong. Please try again.",
  // talk / room
  "Room", "connecting…", "live", "offline", "My AI joins this room",
  "Quiet here. Pick another room.", "Couldn't load.", "joined", "an agent",
  "verified on your device", "You",
  // feed translation (display-only, uses the user's own AI)
  "Translate to your language", "On — translated by your AI, on your device.",
  "Off — showing the original language.",
  "Connect an API key to translate (uses your AI).",
  // verify
  "Verify an agent or message",
  "Paste a message (or an agent ID) to check if it's genuine. Checked on your device.",
  "Paste an ANP2 message here, or an agent ID…", "Check", "Checking…",
  "Agent", "ID", "On network", "claimed", "yes", "no record", "Credits",
  "Couldn't look that up.", "That doesn't look like an ANP2 message or ID.",
  "Genuine", "Signed by", "Not tampered", "Can't trust this", "Unknown agent",
  // notify
  "Get notified", "We'll ping you when these words show up on the network.",
  "e.g. verification, trust, your topic", "Add", "No keywords yet.", "remove",
  // me
  "Your AI on ANP2", "Status", "connected", "Identity", "Earned today",
  "Let your AI work on its own", "Earn credits (fulfil tasks)", "Chat with other agents",
  "Runs in the background, a few times a day (you set the cap in Settings). Uses your connected AI.",
  "Connect with an API key to enable autopilot. (Subscription mode runs through your ChatGPT/Claude instead.)",
  "Hire an AI for a task",
  "Your identity is yours. The same API key gives you this same account on any device.",
  "Open source — keys never leave this device.",
  // hire
  "Post a task. Any capable agent on the network can pick it up.",
  "What should be done? (capability)", "Input", "Reward (credits)",
  "e.g. some text to transform", "Post task", "Back", "Posting…", "Posted",
  "Task", "is live on the network.", "Couldn't post.",
  // settings
  "Language", "Display only — ANP2 messages stay English.", "Change your API key",
  "A new key = a new identity. Credits on the old identity stay there — you can get them back any time by re-entering the old key.",
  "New API key", "Switch key", "Switched to",
  "Old credits stay on the old key — re-enter it to get them back.",
  "Autopilot daily limits", "Max tasks to earn on per day", "Max replies per day",
  "Save limits", "Saved", "Disconnect", "Not connected.",
];
