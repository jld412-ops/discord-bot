# Discord Music Bot with Last.fm

A music bot for a small server. It plays songs from YouTube (and anything else yt-dlp supports), and can build queues from people's Last.fm accounts.

## Setup

1. **Install FFmpeg** (the bot needs it to stream audio):
   ```
   winget install Gyan.FFmpeg
   ```
   Open a new terminal afterwards so `ffmpeg` is on your PATH.

2. **Create the Discord bot**
   - Go to https://discord.com/developers/applications, click **New Application**, then open **Bot** and click **Reset Token** to get a token.
   - Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes, plus the `Connect`, `Speak`, `Send Messages` and `Embed Links` permissions. Open the generated URL to invite the bot to your server.

3. **Get a Last.fm API key** at https://www.last.fm/api/account/create. Only the API key is needed.

4. **Configure the bot:** copy `.env.example` to `.env` and fill it in. Set `GUILD_ID` to your server's ID (right-click the server → Copy Server ID, with Developer Mode on) so the commands show up right away.

5. **Install and run:**
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python bot.py
   ```

## Commands

### Playback
| Command | What it does |
|---|---|
| `/play <song or URL>` | Search YouTube or play a link. Playlist links queue the whole playlist (up to 100 songs). |
| `/skip`, `/pause`, `/resume`, `/stop` | Control playback. `/stop` clears the queue and leaves the voice channel. |
| `/queue [page]`, `/nowplaying` | See what's playing and what's up next. |
| `/shuffle`, `/remove <position>`, `/clear` | Manage the queue. |
| `/volume <0-100>` | Set the volume. |

### Last.fm account
| Command | What it does |
|---|---|
| `/lastfm link <username>` | Connect your Last.fm account (the bot checks that it exists). |
| `/lastfm unlink` | Disconnect it. |
| `/lastfm profile [member]` | Show scrobbles, current track and top artists. |

### Play from Last.fm
Commands with a `member` option can use anyone's linked account, e.g. `/fm top member:@friend`.

| Command | What it does |
|---|---|
| `/fm top [period] [count] [member] [shuffle]` | Your most-played tracks (last 7 days to all time). |
| `/fm recent [count] [member]` | Recently scrobbled tracks. |
| `/fm loved [count] [member] [shuffle]` | Loved tracks. |
| `/fm nowplaying [member]` | Queue whatever that person is scrobbling right now. |
| `/fm similar [artist] [track] [count]` | Tracks similar to a song. With no arguments it uses the current Last.fm-queued song, or else your latest scrobble. |
| `/fm artist <artist> [count] [shuffle]` | An artist's most popular tracks. |
| `/fm tag <tag> [count]` | Top tracks for a genre or tag, like `shoegaze`, `90s` or `jazz`. |
| `/fm discover [period] [count] [member]` | Songs from artists similar to your favorites that you don't already listen to much. |

## Notes
- The bot leaves voice after 5 minutes with an empty queue, or when everyone leaves the channel.
- Last.fm links are saved in `data/lastfm_links.json`.
- If YouTube playback starts failing, update yt-dlp first: `pip install -U "yt-dlp[default]"`. YouTube changes often, and a newer yt-dlp usually fixes it. Some YouTube features also need a JavaScript runtime installed, such as Deno (`winget install DenoLand.Deno`).
