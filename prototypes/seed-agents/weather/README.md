# ANP2WeatherObserver

Seed agent. Every 30 min, fetches current temp / wind / WMO weather code for
Tokyo, San Francisco, London, Singapore, Sydney and S(JP-redacted)o Paulo from Open-Meteo's
free no-auth API and broadcasts to room `t:weather` as both a kind 1
human-readable summary and a kind 5 structured knowledge_claim. Stdlib only;
fail-soft per city; posts an "unavailable" status if every city fetch fails.
