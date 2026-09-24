from __future__ import annotations

import asyncio
import json
import os
import random
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import Music, Track, UserFacingError

API_URL = "https://ws.audioscrobbler.com/2.0/"
LINKS_FILE = Path(__file__).resolve().parent.parent / "data" / "lastfm_links.json"

PERIODS = [
    app_commands.Choice(name="Last 7 days", value="7day"),
    app_commands.Choice(name="Last month", value="1month"),
    app_commands.Choice(name="Last 3 months", value="3month"),
    app_commands.Choice(name="Last 6 months", value="6month"),
    app_commands.Choice(name="Last year", value="12month"),
    app_commands.Choice(name="All time", value="overall"),
]


def _as_list(value: Any) -> list:
    """Last.fm returns a bare object instead of a list when there's only one result."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _artist_name(track: dict) -> str:
    artist = track.get("artist", "")
    if isinstance(artist, dict):
        return artist.get("name") or artist.get("#text") or ""
    return artist


class LastFMClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.session: aiohttp.ClientSession | None = None

    async def call(self, method: str, **params: Any) -> dict:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"User-Agent": "discord-music-bot/1.0"})
        query = {"method": method, "api_key": self.api_key, "format": "json"}
        query.update({k: str(v) for k, v in params.items() if v is not None})
        async with self.session.get(API_URL, params=query) as resp:
            try:
                data = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                raise UserFacingError(f"Last.fm returned an unexpected response (HTTP {resp.status}).")
        if "error" in data:
            raise UserFacingError(f"Last.fm: {data.get('message', 'unknown error')}")
        return data

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    # Each helper returns a list of (artist, track name) pairs, or plain names for artists.

    async def user_info(self, user: str) -> dict:
        return (await self.call("user.getInfo", user=user))["user"]

    async def top_tracks(self, user: str, period: str, limit: int) -> list[tuple[str, str]]:
        data = await self.call("user.getTopTracks", user=user, period=period, limit=limit)
        return [(_artist_name(t), t["name"]) for t in _as_list(data["toptracks"].get("track"))]

    async def recent_tracks(self, user: str, limit: int) -> list[dict]:
        data = await self.call("user.getRecentTracks", user=user, limit=limit)
        return _as_list(data["recenttracks"].get("track"))

    async def loved_tracks(self, user: str, limit: int) -> list[tuple[str, str]]:
        data = await self.call("user.getLovedTracks", user=user, limit=limit)
        return [(_artist_name(t), t["name"]) for t in _as_list(data["lovedtracks"].get("track"))]

    async def similar_tracks(self, artist: str, track: str, limit: int) -> list[tuple[str, str]]:
        data = await self.call("track.getSimilar", artist=artist, track=track, limit=limit, autocorrect=1)
        return [(_artist_name(t), t["name"]) for t in _as_list(data["similartracks"].get("track"))]

    async def artist_top_tracks(self, artist: str, limit: int) -> list[tuple[str, str]]:
        data = await self.call("artist.getTopTracks", artist=artist, limit=limit, autocorrect=1)
        return [(_artist_name(t), t["name"]) for t in _as_list(data["toptracks"].get("track"))]

    async def tag_top_tracks(self, tag: str, limit: int) -> list[tuple[str, str]]:
        data = await self.call("tag.getTopTracks", tag=tag, limit=limit)
        return [(_artist_name(t), t["name"]) for t in _as_list(data["tracks"].get("track"))]

    async def top_artists(self, user: str, period: str, limit: int) -> list[str]:
        data = await self.call("user.getTopArtists", user=user, period=period, limit=limit)
        return [a["name"] for a in _as_list(data["topartists"].get("artist"))]

    async def similar_artists(self, artist: str, limit: int) -> list[str]:
        data = await self.call("artist.getSimilar", artist=artist, limit=limit, autocorrect=1)
        return [a["name"] for a in _as_list(data["similarartists"].get("artist"))]


class LinkStore:
    """Maps Discord user IDs to Last.fm usernames, saved in a JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.links: dict[str, str] = json.loads(path.read_text("utf-8")) if path.exists() else {}

    def get(self, user_id: int) -> str | None:
        return self.links.get(str(user_id))

    def set(self, user_id: int, username: str | None) -> None:
        if username is None:
            self.links.pop(str(user_id), None)
        else:
            self.links[str(user_id)] = username
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.links, indent=2), "utf-8")


class LastFM(commands.Cog):
    lastfm = app_commands.Group(name="lastfm", description="Connect your Last.fm account")
    fm = app_commands.Group(name="fm", description="Play music based on Last.fm")

    def __init__(self, bot: commands.Bot, api_key: str) -> None:
        self.bot = bot
        self.client = LastFMClient(api_key)
        self.store = LinkStore(LINKS_FILE)

    async def cog_unload(self) -> None:
        await self.client.close()

    # ---- helpers ----

    @property
    def music(self) -> Music:
        return self.bot.get_cog("Music")  # type: ignore[return-value]

    def resolve_user(self, interaction: discord.Interaction, member: discord.Member | None) -> str:
        target = member or interaction.user
        username = self.store.get(target.id)
        if username:
            return username
        if target == interaction.user:
            raise UserFacingError("You haven't linked Last.fm yet. Use `/lastfm link` first.")
        raise UserFacingError(f"{target.display_name} hasn't linked a Last.fm account.")

    async def queue_pairs(
        self,
        interaction: discord.Interaction,
        pairs: list[tuple[str, str]],
        description: str,
        shuffle: bool = False,
    ) -> None:
        # De-duplicate while keeping order.
        seen: set[tuple[str, str]] = set()
        unique = []
        for artist, name in pairs:
            key = (artist.lower(), name.lower())
            if artist and name and key not in seen:
                seen.add(key)
                unique.append((artist, name))
        if not unique:
            raise UserFacingError("Last.fm didn't return any tracks for that.")
        if shuffle:
            random.shuffle(unique)

        tracks = [Track.from_lastfm(artist, name, interaction.user) for artist, name in unique]
        await self.music.enqueue(interaction, tracks)

        preview = "\n".join(f"`{i}.` {t.title}" for i, t in enumerate(tracks[:10], 1))
        if len(tracks) > 10:
            preview += f"\n…and {len(tracks) - 10} more"
        embed = discord.Embed(title=f"➕ Queued {len(tracks)} track(s)", description=preview, color=0xD51007)
        embed.set_footer(text=description)
        await interaction.followup.send(embed=embed)

    # ---- /lastfm: account linking ----

    @lastfm.command(name="link", description="Connect your Last.fm account to the bot.")
    @app_commands.describe(username="Your Last.fm username")
    async def link(self, interaction: discord.Interaction, username: str) -> None:
        await interaction.response.defer(ephemeral=True)
        info = await self.client.user_info(username.strip())
        self.store.set(interaction.user.id, info["name"])
        await interaction.followup.send(
            f"✅ Linked to Last.fm account **[{info['name']}]({info['url']})** "
            f"({int(info.get('playcount', 0)):,} scrobbles).",
            ephemeral=True,
        )

    @lastfm.command(name="unlink", description="Disconnect your Last.fm account.")
    async def unlink(self, interaction: discord.Interaction) -> None:
        if not self.store.get(interaction.user.id):
            raise UserFacingError("You don't have a linked Last.fm account.")
        self.store.set(interaction.user.id, None)
        await interaction.response.send_message("Unlinked your Last.fm account.", ephemeral=True)

    @lastfm.command(name="profile", description="Show a linked Last.fm profile.")
    @app_commands.describe(member="Whose profile (defaults to you)")
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        username = self.resolve_user(interaction, member)
        await interaction.response.defer()
        info, recent, top = await asyncio.gather(
            self.client.user_info(username),
            self.client.recent_tracks(username, 1),
            self.client.top_artists(username, "1month", 5),
        )
        embed = discord.Embed(title=info["name"], url=info["url"], color=0xD51007)
        images = [i["#text"] for i in _as_list(info.get("image")) if i.get("#text")]
        if images:
            embed.set_thumbnail(url=images[-1])
        embed.add_field(name="Scrobbles", value=f"{int(info.get('playcount', 0)):,}")
        if recent:
            t = recent[0]
            label = "Listening now" if t.get("@attr", {}).get("nowplaying") else "Last played"
            embed.add_field(name=label, value=f"{_artist_name(t)} - {t['name']}", inline=False)
        if top:
            embed.add_field(name="Top artists this month", value=", ".join(top), inline=False)
        await interaction.followup.send(embed=embed)

    # ---- /fm: play music from Last.fm ----

    @fm.command(name="top", description="Play your (or someone's) most-played tracks.")
    @app_commands.describe(
        period="Time range", count="How many tracks", member="Use someone else's account", shuffle="Shuffle them"
    )
    @app_commands.choices(period=PERIODS)
    async def top(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        count: app_commands.Range[int, 1, 50] = 15,
        member: discord.Member | None = None,
        shuffle: bool = False,
    ) -> None:
        username = self.resolve_user(interaction, member)
        await interaction.response.defer()
        period_value = period.value if period else "1month"
        pairs = await self.client.top_tracks(username, period_value, count)
        label = period.name if period else "Last month"
        await self.queue_pairs(interaction, pairs, f"{username}'s top tracks · {label}", shuffle)

    @fm.command(name="recent", description="Play recently scrobbled tracks.")
    @app_commands.describe(count="How many tracks", member="Use someone else's account")
    async def recent(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 50] = 10,
        member: discord.Member | None = None,
    ) -> None:
        username = self.resolve_user(interaction, member)
        await interaction.response.defer()
        items = await self.client.recent_tracks(username, count)
        pairs = [(_artist_name(t), t["name"]) for t in items][:count]
        await self.queue_pairs(interaction, pairs, f"{username}'s recent scrobbles")

    @fm.command(name="loved", description="Play loved tracks.")
    @app_commands.describe(count="How many tracks", member="Use someone else's account", shuffle="Shuffle them")
    async def loved(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 50] = 20,
        member: discord.Member | None = None,
        shuffle: bool = True,
    ) -> None:
        username = self.resolve_user(interaction, member)
        await interaction.response.defer()
        pairs = await self.client.loved_tracks(username, count)
        await self.queue_pairs(interaction, pairs, f"{username}'s loved tracks", shuffle)

    @fm.command(name="nowplaying", description="Play the song someone is scrobbling right now.")
    @app_commands.describe(member="Whose current song (defaults to you)")
    async def fm_nowplaying(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        username = self.resolve_user(interaction, member)
        await interaction.response.defer()
        items = await self.client.recent_tracks(username, 1)
        if not items:
            raise UserFacingError(f"{username} hasn't scrobbled anything yet.")
        t = items[0]
        playing = t.get("@attr", {}).get("nowplaying")
        note = "listening to right now" if playing else "last played"
        await self.queue_pairs(interaction, [(_artist_name(t), t["name"])], f"What {username} is {note}")

    @fm.command(name="similar", description="Play tracks similar to a song (defaults to the current one).")
    @app_commands.describe(artist="Artist name", track="Track name", count="How many tracks")
    async def similar(
        self,
        interaction: discord.Interaction,
        artist: str | None = None,
        track: str | None = None,
        count: app_commands.Range[int, 1, 50] = 15,
    ) -> None:
        await interaction.response.defer()
        if not (artist and track):
            current = self.music.current_track(interaction.guild)
            if current and current.artist and current.track_name:
                artist, track = current.artist, current.track_name
            elif username := self.store.get(interaction.user.id):
                items = await self.client.recent_tracks(username, 1)
                if items:
                    artist, track = _artist_name(items[0]), items[0]["name"]
        if not (artist and track):
            raise UserFacingError("Give me an `artist` and `track`, or link Last.fm so I can use your last scrobble.")
        pairs = await self.client.similar_tracks(artist, track, count)
        await self.queue_pairs(interaction, pairs, f"Similar to {artist} - {track}")

    @fm.command(name="artist", description="Play an artist's most popular tracks.")
    @app_commands.describe(artist="Artist name", count="How many tracks", shuffle="Shuffle them")
    async def artist(
        self,
        interaction: discord.Interaction,
        artist: str,
        count: app_commands.Range[int, 1, 50] = 10,
        shuffle: bool = False,
    ) -> None:
        await interaction.response.defer()
        pairs = await self.client.artist_top_tracks(artist, count)
        await self.queue_pairs(interaction, pairs, f"Top tracks by {artist}", shuffle)

    @fm.command(name="tag", description="Play top tracks for a genre/tag, e.g. 'shoegaze' or '80s'.")
    @app_commands.describe(tag="Genre or tag", count="How many tracks")
    async def tag(
        self,
        interaction: discord.Interaction,
        tag: str,
        count: app_commands.Range[int, 1, 50] = 15,
    ) -> None:
        await interaction.response.defer()
        # Pull a bigger pool and sample from it so the same tag doesn't always give the same list.
        pool = await self.client.tag_top_tracks(tag, min(count * 3, 150))
        pairs = random.sample(pool, min(count, len(pool)))
        await self.queue_pairs(interaction, pairs, f"Top tracks tagged '{tag}'")

    @fm.command(name="discover", description="Play songs from artists similar to your favorites that you haven't played much.")
    @app_commands.describe(period="Which favorites to base it on", count="How many tracks", member="Use someone else's account")
    @app_commands.choices(period=PERIODS)
    async def discover(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        count: app_commands.Range[int, 1, 20] = 10,
        member: discord.Member | None = None,
    ) -> None:
        username = self.resolve_user(interaction, member)
        await interaction.response.defer()
        favorites = await self.client.top_artists(username, period.value if period else "3month", 50)
        if not favorites:
            raise UserFacingError(f"{username} doesn't have enough listening history yet.")
        known = {a.lower() for a in favorites}

        seeds = random.sample(favorites[:15], min(5, len(favorites[:15])))
        results = await asyncio.gather(
            *(self.client.similar_artists(a, 15) for a in seeds), return_exceptions=True
        )
        candidates = list(dict.fromkeys(
            name for r in results if isinstance(r, list) for name in r if name.lower() not in known
        ))
        if not candidates:
            raise UserFacingError("Couldn't find any new artists to recommend. Try a different period.")
        picked = random.sample(candidates, min(count, len(candidates)))

        top_lists = await asyncio.gather(
            *(self.client.artist_top_tracks(a, 5) for a in picked), return_exceptions=True
        )
        pairs = [random.choice(r) for r in top_lists if isinstance(r, list) and r]
        await self.queue_pairs(interaction, pairs, f"Discovery mix for {username}")


async def setup(bot: commands.Bot) -> None:
    api_key = os.getenv("LASTFM_API_KEY")
    if not api_key:
        raise RuntimeError("LASTFM_API_KEY is not set. Get one at https://www.last.fm/api/account/create")
    await bot.add_cog(LastFM(bot, api_key))
