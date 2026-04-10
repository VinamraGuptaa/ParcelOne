"""
Entry point: python server.py

Reads PORT from environment (Render injects this automatically).
"""

import os
import logging

from dotenv import load_dotenv

load_dotenv()  # Load .env for local development

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

from api.app import create_app  # noqa: E402 — import after dotenv loaded

import uvicorn  # noqa: E402

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=os.environ.get("DEV", "") == "1",
    )
