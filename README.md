# Social DMs

## Overview

Social DMs is a hybrid agentic framework for archiving, analyzing, and routing high-volume social direct messages.

## What it does

- Connects to Meta/Instagram-style message sources where configured.
- Archives inbound messages for later review.
- Supports semantic analysis and routing of conversations.
- Can combine cloud LLMs with local inference for classification and summarization.
- Supports secure local webhook exposure through tunneling when needed.

## User workflows

- Configure provider credentials and webhook/tunnel settings.
- Start the API or worker services.
- Ingest messages into local storage.
- Review routed/classified messages and generated summaries.
- Iterate on routing rules and model selection.

## Stack

- Python application.
- FastAPI/Uvicorn runtime.
- Playwright and browser automation dependencies.
- Telegram, Instagram, and media tooling dependencies.
- Optional vector database and local/cloud LLM integrations.
- Docker Compose for containerized runtime.

## Project structure

- `src/` - application source.
- `tests/` - tests.
- `requirements.txt` - Python dependencies.
- `docker-compose.yml` - local service runtime.
- `Dockerfile` - container build.

## Setup

- Create a Python virtual environment.
- Install dependencies with `pip install -r requirements.txt`.
- Create a local `.env` file for provider credentials and runtime settings.
- Use Docker Compose if running the full local service stack.

## Common commands

- `pip install -r requirements.txt` - install Python dependencies.
- `docker compose up` - start containerized services.
- `python -m pytest` - run tests when test dependencies are installed.
- `uvicorn src.main:app --reload` - likely local API pattern; confirm the app entrypoint before relying on it.

## Configuration and secrets

- Provider tokens, API keys, webhook secrets, tunnel credentials, and account credentials must stay in local env files or provider dashboards.
- Do not document real token values.
- Keep social-message archives private.

## Data, storage, and integrations

- Message archives and vector-store data are local/private project data.
- External integrations may include Meta Graph API, Telegram, local vector databases, Cloudflare Tunnel, and LLM providers.

## Troubleshooting

- If webhooks do not arrive, verify tunnel URL, provider callback config, and local port.
- If browser automation fails, reinstall Playwright browsers.
- If analysis fails, check the configured model provider and API key or local model runtime.

## Documentation maintenance

Update this README whenever functionality, setup, commands, environment variables, storage, integrations, or user workflows change. Follow the CoS project documentation standard in `../../../docs/project-documentation-standard.md`. Never include real secret values.
