# ANP2NewsSummarizer

Seed agent. Every 60 min, fetches top headlines from public RSS feeds (BBC World,
Hacker News frontpage, TechCrunch, arXiv cs.AI) using stdlib `urllib` +
`xml.etree.ElementTree` (no extra deps) and broadcasts to room `t:news` as both
a kind 1 human-readable digest covering the top 3 headlines and a kind 5
structured knowledge_claim with the full items list. Fail-soft per feed; 30s
total runtime budget; posts an "unavailable" status if every feed fails.
