"""Simple Discord bot that forwards messages to the NYC dispatch webhook.

No LLM needed — just forwards the message text, the webhook handles
geocoding and categorization.

Run: PYTHONPATH=src python -m hackathon_nyc.discord_bot
"""

import os
import aiohttp
import discord

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
WEBHOOK_URL = "http://localhost:8000/api/webhook/report"
SUBSCRIBE_URL = "http://localhost:8000/api/alerts/subscribe"

# Whether a message is a report is decided by intake.looks_like_report, which
# shares the category rules the rest of the system uses. This module used to
# keep its own keyword list, which drifted from the others.
from hackathon_nyc import intake

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"NYC Dispatch Bot online as {client.user}")
    await client.change_presence(activity=discord.CustomActivity(name="NYC Urban Intelligence"))


@client.event
async def on_message(message):
    # Ignore own messages
    if message.author == client.user:
        return

    # Only respond to DMs or when mentioned in a server
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = client.user in message.mentions
    if not is_dm and not is_mentioned:
        return

    text = message.content.replace(f"<@{client.user.id}>", "").strip()
    if not text:
        return

    text_lower = text.lower()

    # Check for alert subscription
    if text_lower.startswith("alert ") or text_lower.startswith("subscribe ") or text_lower.startswith("notify "):
        address = text[text.index(" ")+1:].strip()
        await message.add_reaction("👀")
        async with aiohttp.ClientSession() as session:
            async with session.post(SUBSCRIBE_URL, json={
                "name": str(message.author),
                "contact": str(message.author.id),
                "contact_type": "discord",
                "address": address,
                "radius_miles": 1.0,
            }) as resp:
                if resp.status == 200:
                    await message.add_reaction("🔔")
                else:
                    await message.add_reaction("❌")
        return

    # Check if it looks like a report
    is_report = intake.looks_like_report(text)
    if not is_report and not is_dm:
        # In servers, ignore non-report messages even if mentioned
        return

    # Transcript/text cleanup happens in intake, not here.

    # Forward to the shared intake endpoint
    await message.add_reaction("👀")

    async with aiohttp.ClientSession() as session:
        async with session.post(WEBHOOK_URL, json={
            "message": text,
            "source": "discord",
            "user": str(message.author),
        }) as resp:
            if resp.status == 200:
                data = await resp.json()
                incident_id = data.get("id", "?")
                await message.add_reaction("✅")
                # Brief confirmation
                addr = data.get("address", "")
                cat = data.get("category", "other")
                short_addr = addr[:40] + "..." if len(addr) > 40 else addr
                # intake supplies the reply, so Discord says the same thing SMS
                # and voice do — including the 911 redirect on life-safety
                # reports and a location prompt when geocoding failed.
                reply = data.get("reply") or f"Incident **#{incident_id}** created — {cat} near {short_addr}"
                if data.get("life_safety"):
                    await message.add_reaction("🚨")
                await message.reply(reply, mention_author=False)
            else:
                await message.add_reaction("❌")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
