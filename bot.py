import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from cogs.music import UserFacingError

load_dotenv()
log = logging.getLogger("bot")


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # includes voice_states
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self) -> None:
        await self.load_extension("cogs.music")
        await self.load_extension("cogs.lastfm")
        self.tree.on_error = self.on_app_command_error

        # Syncing to a single guild is instant; global sync can take up to an hour.
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, UserFacingError):
            error = error.original
        if isinstance(error, (UserFacingError, app_commands.CheckFailure)):
            message = str(error)
        else:
            log.exception("Command error", exc_info=error)
            message = "Something went wrong running that command."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    discord.utils.setup_logging(level=logging.INFO)
    MusicBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
