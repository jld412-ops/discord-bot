from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("music")

IDLE_TIMEOUT = 300  # seconds with an empty queue before the bot leaves voice
MAX_PLAYLIST_ITEMS = 100

YDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",  # avoid IPv6 issues
}
YDL_STREAM = {**YDL_BASE, "format": "bestaudio/best", "noplaylist": True}
YDL_FLAT = {**YDL_BASE, "extract_flat": "in_playlist", "playlistend": MAX_PLAYLIST_ITEMS}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class UserFacingError(app_commands.AppCommandError):
    """An error whose message is safe to show to the user as-is."""


@dataclass
class Track:
    query: str  # URL, or search text resolved at play time
    title: str
    requester: discord.abc.User
    url: str | None = None
    duration: int | None = None
    # Set for tracks that came from Last.fm, so /fm similar can use them.
    artist: str | None = None
    track_name: str | None = None

    @classmethod
    def from_lastfm(cls, artist: str, name: str, requester: discord.abc.User) -> Track:
        return cls(
            query=f"{artist} - {name} audio",
            title=f"{artist} - {name}",
            requester=requester,
            artist=artist,
            track_name=name,
        )


def fmt_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "?:??"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def _search_flat(query: str) -> list[dict]:
    """Look up a URL, playlist URL or search text without fetching stream URLs."""
    target = query if _is_url(query) else f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(YDL_FLAT) as ydl:
        info = ydl.extract_info(target, download=False)
    if info is None:
        return []
    if "entries" in info:
        return [e for e in info["entries"] if e]
    return [info]


def _extract_stream(query: str) -> dict:
    """Resolve a track to a playable stream. Stream URLs expire, so do this right before playing."""
    target = query if _is_url(query) else f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(YDL_STREAM) as ydl:
        info = ydl.extract_info(target, download=False)
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else None
    if not info:
        raise LookupError(f"No results for {query!r}")
    return info


class GuildPlayer:
    """Queue and playback loop for one server."""

    def __init__(self, cog: Music, guild: discord.Guild, channel: discord.abc.Messageable) -> None:
        self.cog = cog
        self.guild = guild
        self.channel = channel
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.volume = 0.5
        self._wake = asyncio.Event()
        self._track_done = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    def add(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)
        self._wake.set()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                if not self.queue:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        await self.channel.send("Queue has been empty for a while, leaving voice. 👋")
                        return
                    continue

                track = self.queue.pop(0)
                vc = self.guild.voice_client
                if not isinstance(vc, discord.VoiceClient) or not vc.is_connected():
                    return

                try:
                    info = await loop.run_in_executor(None, _extract_stream, track.query)
                except Exception as e:
                    log.warning("Failed to resolve %s: %s", track.query, e)
                    await self.channel.send(f"⚠️ Couldn't play **{track.title}**, skipping.")
                    continue

                track.title = info.get("title") or track.title
                track.url = info.get("webpage_url") or track.url
                track.duration = info.get("duration") or track.duration
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTS), volume=self.volume
                )

                self._track_done.clear()
                self.current = track

                def after(err: Exception | None) -> None:
                    if err:
                        log.error("Playback error: %s", err)
                    loop.call_soon_threadsafe(self._track_done.set)

                vc.play(source, after=after)
                await self.channel.send(embed=self.now_playing_embed())
                await self._track_done.wait()
                self.current = None
        finally:
            self.current = None
            # If cleanup() already replaced/removed us, don't tear down a newer player.
            if self.cog.players.get(self.guild.id) is self:
                await self.cog.cleanup(self.guild)

    def now_playing_embed(self) -> discord.Embed:
        t = self.current
        if t is None:
            return discord.Embed(description="Nothing is playing.")
        embed = discord.Embed(title="Now playing", description=f"[{t.title}]({t.url})" if t.url else t.title)
        embed.add_field(name="Length", value=fmt_duration(t.duration))
        embed.add_field(name="Requested by", value=t.requester.mention)
        embed.add_field(name="Up next", value=f"{len(self.queue)} track(s)")
        return embed


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    async def cog_unload(self) -> None:
        for guild_id in list(self.players):
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.cleanup(guild)

    # ---- shared helpers (also used by the Last.fm cog) ----

    async def cleanup(self, guild: discord.Guild) -> None:
        player = self.players.pop(guild.id, None)
        if player and player.task is not asyncio.current_task():
            player.task.cancel()
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)

    async def get_player(self, interaction: discord.Interaction, *, connect: bool = True) -> GuildPlayer:
        """Return this server's player, joining the caller's voice channel if needed."""
        guild = interaction.guild
        if guild is None:
            raise UserFacingError("This command only works in a server.")
        member = interaction.user
        user_channel = member.voice.channel if isinstance(member, discord.Member) and member.voice else None
        vc = guild.voice_client

        if vc is None:
            if not connect:
                raise UserFacingError("I'm not playing anything right now.")
            if user_channel is None:
                raise UserFacingError("Join a voice channel first.")
            await user_channel.connect(self_deaf=True)
        elif user_channel != vc.channel:
            raise UserFacingError(f"You need to be in {vc.channel.mention} to control the music.")

        player = self.players.get(guild.id)
        if player is None:
            player = GuildPlayer(self, guild, interaction.channel)
            self.players[guild.id] = player
        return player

    async def enqueue(self, interaction: discord.Interaction, tracks: list[Track]) -> GuildPlayer:
        player = await self.get_player(interaction)
        player.add(tracks)
        return player

    def current_track(self, guild: discord.Guild | None) -> Track | None:
        player = self.players.get(guild.id) if guild else None
        return player.current if player else None

    # ---- listeners ----

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        vc = guild.voice_client
        if member.id == self.bot.user.id and after.channel is None:
            # Bot was disconnected (kicked or /stop); make sure state is cleared.
            player = self.players.pop(guild.id, None)
            if player:
                player.task.cancel()
            return
        # Leave if everyone else has left the bot's channel.
        if vc and before.channel == vc.channel and after.channel != vc.channel:
            if not any(not m.bot for m in vc.channel.members):
                player = self.players.get(guild.id)
                if player:
                    await player.channel.send("Everyone left the voice channel, so I stopped. 👋")
                await self.cleanup(guild)

    # ---- commands ----

    @app_commands.command(description="Play a song by name, or a YouTube/SoundCloud link or playlist.")
    @app_commands.describe(query="Song name or URL")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        player = await self.get_player(interaction)
        try:
            entries = await asyncio.get_running_loop().run_in_executor(None, _search_flat, query)
        except yt_dlp.utils.DownloadError as e:
            raise UserFacingError(f"Couldn't load that: {e.msg or e}") from e
        if not entries:
            raise UserFacingError(f"No results for **{query}**.")

        tracks = [
            Track(
                query=e.get("webpage_url") or e.get("url") or query,
                title=e.get("title") or query,
                requester=interaction.user,
                url=e.get("webpage_url") or e.get("url"),
                duration=e.get("duration"),
            )
            for e in entries
        ]
        was_idle = player.current is None and not player.queue
        player.add(tracks)

        if len(tracks) > 1:
            await interaction.followup.send(f"➕ Added **{len(tracks)}** tracks to the queue.")
        elif was_idle:
            await interaction.followup.send(f"🎶 Loading **{tracks[0].title}**…")
        else:
            await interaction.followup.send(
                f"➕ Queued **{tracks[0].title}** ({fmt_duration(tracks[0].duration)}), position {len(player.queue)}."
            )

    @app_commands.command(description="Skip the current song.")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self.get_player(interaction, connect=False)
        vc = interaction.guild.voice_client
        if not vc.is_playing() and not vc.is_paused():
            raise UserFacingError("Nothing is playing.")
        vc.stop()  # triggers the `after` callback, which advances the queue
        await interaction.response.send_message("⏭️ Skipped.")

    @app_commands.command(description="Pause playback.")
    async def pause(self, interaction: discord.Interaction) -> None:
        await self.get_player(interaction, connect=False)
        vc = interaction.guild.voice_client
        if not vc.is_playing():
            raise UserFacingError("Nothing is playing.")
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.")

    @app_commands.command(description="Resume playback.")
    async def resume(self, interaction: discord.Interaction) -> None:
        await self.get_player(interaction, connect=False)
        vc = interaction.guild.voice_client
        if not vc.is_paused():
            raise UserFacingError("Playback isn't paused.")
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")

    @app_commands.command(description="Stop playing, clear the queue and leave voice.")
    async def stop(self, interaction: discord.Interaction) -> None:
        await self.get_player(interaction, connect=False)
        await self.cleanup(interaction.guild)
        await interaction.response.send_message("⏹️ Stopped and left the channel.")

    @app_commands.command(name="nowplaying", description="Show the current song.")
    async def now_playing(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None or player.current is None:
            raise UserFacingError("Nothing is playing.")
        await interaction.response.send_message(embed=player.now_playing_embed())

    @app_commands.command(description="Show the queue.")
    @app_commands.describe(page="Page number")
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None or (player.current is None and not player.queue):
            raise UserFacingError("The queue is empty.")
        per_page = 10
        pages = max(1, -(-len(player.queue) // per_page))
        page = min(page, pages)
        start = (page - 1) * per_page
        lines = [
            f"`{i}.` {t.title} ({fmt_duration(t.duration)}) · {t.requester.display_name}"
            for i, t in enumerate(player.queue[start : start + per_page], start=start + 1)
        ]
        embed = discord.Embed(title="Queue")
        if player.current:
            embed.add_field(name="Now playing", value=player.current.title, inline=False)
        embed.add_field(name="Up next", value="\n".join(lines) or "Nothing queued.", inline=False)
        embed.set_footer(text=f"Page {page}/{pages} · {len(player.queue)} track(s)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Shuffle the queue.")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction, connect=False)
        if len(player.queue) < 2:
            raise UserFacingError("Not enough songs in the queue to shuffle.")
        random.shuffle(player.queue)
        await interaction.response.send_message("🔀 Shuffled the queue.")

    @app_commands.command(description="Remove a song from the queue.")
    @app_commands.describe(position="Position in /queue")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]) -> None:
        player = await self.get_player(interaction, connect=False)
        if position > len(player.queue):
            raise UserFacingError(f"The queue only has {len(player.queue)} track(s).")
        track = player.queue.pop(position - 1)
        await interaction.response.send_message(f"🗑️ Removed **{track.title}**.")

    @app_commands.command(description="Clear the queue (keeps the current song playing).")
    async def clear(self, interaction: discord.Interaction) -> None:
        player = await self.get_player(interaction, connect=False)
        player.queue.clear()
        await interaction.response.send_message("🧹 Cleared the queue.")

    @app_commands.command(description="Set the volume.")
    @app_commands.describe(percent="0 to 100")
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]) -> None:
        player = await self.get_player(interaction, connect=False)
        player.volume = percent / 100
        source = interaction.guild.voice_client.source
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = player.volume
        await interaction.response.send_message(f"🔊 Volume set to {percent}%.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
