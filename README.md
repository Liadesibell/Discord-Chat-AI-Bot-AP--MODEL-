# Discord AI Chat Bot

A Discord bot that responds when a user mentions the bot or says its configured
name. It uses NVIDIA's OpenAI-compatible chat-completions API and keeps
per-Discord-user conversation history in SQLite.

## Setup

1. Use Python 3.10 or newer and install dependencies:

   ```powershell
   py -m pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`, then set `DISCORD_TOKEN` and
   `NVIDIA_API_KEY`. Never commit `.env` or paste API keys into source code.
3. In the Discord Developer Portal, enable **Message Content Intent** for the
   bot, and invite the bot with permission to view and send messages.
4. Start the bot by double-clicking `backend_start.bat`, or run it from a
   terminal:

   ```powershell
   py main.py
   ```

   The batch file uses the project's `.venv` Python when available and keeps
   the console open so startup errors remain visible. Only one bot instance
   using the same Discord token can run at a time. Responses are kept concise
   by default and sent as a single Discord message.

By default, the bot responds to `Nemotron` or a direct @mention. Change
`BOT_NAME` in `.env` to use another name. Users' messages and replies are keyed
by their Discord user ID. For model context, the bot sends the first 20
whitespace-separated words of each of the most recent 20 messages; full messages
remain stored in SQLite. A user's stored history and activity record are
deleted when they have not invoked the bot for 3 days; expired records are
checked at startup and once per hour.

The database is local to the machine running the bot. Back it up only if you
intend to retain conversation data, and restrict access to the database file.
