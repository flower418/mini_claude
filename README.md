# mini_claude

A small Claude-style coding agent loop built with the Anthropic Messages API.

## Setup

```sh
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with:

```sh
ANTHROPIC_API_KEY=...
MODEL_ID=...
```

Then run:

```sh
python agent.py
```
