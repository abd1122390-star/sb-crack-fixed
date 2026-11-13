# Telegram Public Website Asset Collector

## Render Background Worker
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Environment variable: `BOT_TOKEN` = your Telegram BotFather token
- `.python-version` pins Python 3.12.8 to avoid the Python 3.14 event-loop startup issue.

## Run locally
`pip install -r requirements.txt`
Set `BOT_TOKEN`, then run `python bot.py`.

The bot packages publicly accessible browser-delivered website files only. It cannot retrieve server-side source code, databases, secrets, private files, login-only content, or bypass access controls. Use only on sites you own or are authorized to archive.
