# CLAUDE.md

## Project

Telegram admin bot for managing ONLYOFFICE test instances (Python, python-telegram-bot).

## Security

- Never read or display contents of `.env` files
- Never read `*.tfvars`, `*.pem`, `*.key` files
- Never output secrets, tokens, API keys, or credentials
- Never commit `.env`, database files, or SSH keys
- If a file may contain secrets, do not read it — ask the user first
