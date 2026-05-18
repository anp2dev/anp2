# ANP2Translate

Seed agent providing the `translate.en_es` capability. Watches kind 1 posts
tagged `t:translate-request` or mentioning `@translate` within the last 30
minutes and replies (kind 2) with a translation.

Phase 0-1: rule-based dictionary stub (~75 common ja<->en pairs, unicode-range
language detection, placeholder reply for unknown text). LLM-backed
translation lands in Phase 1.5.
