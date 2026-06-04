# mini_claude

A small Claude-style coding agent loop built with the Anthropic Messages API.

## Setup

```sh
pip install -r requirements.txt
```

Create a local `.env` file in the project root:

```env
ANTHROPIC_API_KEY=your_anthropic_api_key_here
MODEL_ID=claude-sonnet-4-6
# Optional:
# ANTHROPIC_BASE_URL=https://api.anthropic.com
```

Do not commit `.env`; it is ignored by Git.

Then run:

```sh
python agent.py
```
