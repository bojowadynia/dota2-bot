# Dota2 bot

WIP. dota2-bot part of https://github.com/bojowadynia/dota-discord-bot which is a fork of https://github.com/sellmark/dota-discord-bot which is a fork of https://github.com/UncleVasya/Dota2-EU-Ladder.


Docs will be in [docs/notes.md](docs/notes.md). Don't expect too much though.

## FAQ

### How to run this thing

Install vscode, docker and devcontainers plugin. Rebuild in container and then run

```sh
python dota_bot.py
```

All the deps will be there. Set env variables like in original project.

### Why did you pull it out of original repo

Cuz django deps in original make any updates to dota protobufs break unrelated stuff and I'm not willing to dive to the bottom of it. Works good enough in 3.12.

### Why gevent

dota2 and steam libraries are set-up around it.

### Why callbacks

dota2 and steam libraries are set-up around it.
