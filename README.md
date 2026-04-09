# MAX_BOT

Python bot for MAX that accepts a message from an admin in a private chat and immediately sends it to selected channels with a saved suffix.

## Features

- Admin whitelist from `admins.txt`
- Immediate sending flow with images
- Inline actions for suffix edit and channel list edit
- Channel selection stored in `channels.txt`
- Suffix stored in `suffix.txt`
- Rotating file logs for hosting

## Local run

```powershell
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python run.py
```

## Hosting

The bot supports environment variables. You can keep local defaults in `config.py`, but for hosting it is better to set:

- `MAX_BOT_TOKEN`
- `ADMIN_USER_IDS_FILE_PATH`
- `CHANNELS_FILE_PATH`
- `SUFFIX_FILE_PATH`
- `LOG_LEVEL`
- `LOG_TO_STDOUT`
- `LOG_TO_FILE`
- `LOG_FILE_PATH`

See [.env.example](/C:/Users/Executor/PycharmProjects/MAX_BOT/.env.example).

## Docker

```bash
docker build -t max-bot .
docker run -d \
  --name max-bot \
  -e MAX_BOT_TOKEN=replace_me \
  -e ADMIN_USER_IDS_FILE_PATH=/app/admins.txt \
  -e LOG_FILE_PATH=/app/logs/bot.log \
  -v $(pwd)/admins.txt:/app/admins.txt \
  -v $(pwd)/channels.txt:/app/channels.txt \
  -v $(pwd)/suffix.txt:/app/suffix.txt \
  -v $(pwd)/logs:/app/logs \
  max-bot
```

If you use Docker, it is better to mount `channels.txt`, `suffix.txt`, and logs as volumes so selections and suffix survive restarts.
Also mount `admins.txt`, because admin access is read from that file.

## Tests

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```
