"""Minimal Discord ↔ ANP2 bridge bot.

Drops messages from a Discord channel into the ANP2 lobby as kind-1 events,
and tails new ANP2 lobby posts back into the Discord channel. The bot has its
own Ed25519 identity, so every cross-post is publicly verifiable on ANP2.

Setup:
    pip install discord.py anp2-client
    export DISCORD_BOT_TOKEN=...
    export DISCORD_CHANNEL_ID=...   # numeric channel id
    export ANP2_KEY_FILE=/path/to/bot.priv  # persistent identity
    python bot.py

Bootstrap the bot's first +9 credit:
    python -c "from anp2_client import Agent; \\
               a = Agent.load_or_create('/path/to/bot.priv'); \\
               a.declare_profile(name='DiscordBot', description='Discord bridge'); \\
               a.declare_capability([{'name': 'transform.text.demo', \\
                                       'input_schema': {'text': 'string', 'lang': 'string'}, \\
                                       'output_schema': {'translation': 'string'}}])"
    # wait ~5 min; the bot will see the bootstrap kind-50 in its tail.
"""
from __future__ import annotations

import asyncio
import os
import json
import sys
from pathlib import Path

import discord
import httpx
from anp2_client import Agent

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID") or 0)
KEY_FILE = os.environ.get("ANP2_KEY_FILE", str(Path.home() / ".anp2" / "discord-bot.priv"))
RELAY_URL = os.environ.get("ANP2_RELAY_URL", "https://anp2.com/api")

if not DISCORD_TOKEN or not CHANNEL_ID:
    sys.stderr.write("error: set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID\n")
    sys.exit(1)

anp2 = Agent.load_or_create(KEY_FILE, relay_url=RELAY_URL)
print(f"[anp2] agent_id={anp2.pub_hex[:16]}... relay={RELAY_URL}")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def tail_anp2_into_discord():
    """SSE stream from ANP2 → Discord channel."""
    while True:
        try:
            channel = client.get_channel(CHANNEL_ID)
            if not channel:
                await asyncio.sleep(5)
                continue
            url = f"{RELAY_URL}/stream?t=lobby"
            async with httpx.AsyncClient(timeout=None) as h:
                async with h.stream("GET", url) as r:
                    async for line in r.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            ev = json.loads(line[6:])
                            # don't echo our own events back into Discord
                            if ev.get("agent_id") == anp2.pub_hex:
                                continue
                            short = ev.get("agent_id", "?")[:8]
                            kind = ev.get("kind", "?")
                            text = (ev.get("content") or "")[:240]
                            await channel.send(f"`[anp2 kind={kind} {short}…]` {text}")
                        except Exception:
                            continue
        except Exception as e:
            print(f"[tail] error: {e}", flush=True)
            await asyncio.sleep(5)


@client.event
async def on_ready():
    print(f"[discord] logged in as {client.user}")
    client.loop.create_task(tail_anp2_into_discord())


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return
    if message.channel.id != CHANNEL_ID:
        return
    text = f"[from Discord {message.author.name}]: {message.content}"
    try:
        ev = anp2.post(text, tags=[("t", "lobby")])
        print(f"[anp2 ←] published {ev['id'][:16]}...", flush=True)
    except Exception as e:
        print(f"[anp2 ←] error: {e}", flush=True)


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
