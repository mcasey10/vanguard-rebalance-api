---
title: Vanguard Rebalance API
emoji: ⚡
colorFrom: red
colorTo: gray
sdk: docker
pinned: false
---

# Vanguard Sell & Rebalance API

A FastAPI backend that solves a real gap in Vanguard's existing product: no tax-consequence reasoning when selling mutual fund holdings. Built as a portfolio project demonstrating Python, API design, and AI integration.

> **Note:** This is a self-initiated portfolio project, not affiliated with Vanguard.

## Demo

▶️ [Watch the API demo on YouTube](https://youtu.be/ScbWNZfhYKs)

## What It Does

Given a portfolio of mutual fund holdings with tax lot detail, the API:

1. **Recommends an optimized sell plan** (Workflow A) — selects which lots to sell using a MinTax strategy: short-term losses first, then long-term losses, then long-term gains, then short-term gains
2. **Calculates real-time tax impact** on any manual sell scenario (Workflow B) — useful when an investor wants to deviate from the recommendation
3. **Explains the recommendation in plain English** using the Anthropic Claude API — takes the optimized sell plan as input and translates the tax lot reasoning into language a non-expert investor would understand. Not applicable to Workflow B, where the investor has chosen their own sell amounts.

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| POST | `/recommend` | Workflow A — MinTax optimized sell recommendation |
| POST | `/scenario` | Workflow B — Real-time tax calculation on manual amounts |
| POST | `/explain` | AI-generated plain-English explanation of a recommendation |

Interactive API docs available at `/docs` (Swagger UI).

## Key Design Decisions

- **MinTax lot priority order**: short-term loss → long-term loss → long-term gain → short-term gain
- **Tax savings attribution**: per-fund FIFO comparison rather than proportional allocation
- **Gain-based tax calculation**: tax applied to gain only, not gross proceeds
- **Workflow A always produces equal or lower effective rate than Workflow B** on identical portfolio data — verified in testing

## Tech Stack

- **Python 3.14**
- **FastAPI** — API framework
- **Pydantic** — data modeling and validation
- **Uvicorn** — ASGI server
- **Anthropic Python SDK** — Claude Sonnet for AI explanation layer

## Sample Results

On a $10,000 withdrawal from a two-fund portfolio:

| Approach | Tax | Effective Rate |
|----------|-----|----------------|
| Workflow A (MinTax optimized) | $351.59 | 3.52% |
| Workflow B (manual deviation) | $623.51 | 6.24% |

**Tax savings: $271.92** by selling the bond fund's loss lot before the stock fund's gain lots.

## Running Locally

```bash
# Clone the repo
git clone https://github.com/mcasey10/vanguard-rebalance-api.git
cd vanguard-rebalance-api

# Create and activate virtual environment
python -m venv venv
venv\Scripts\Activate.ps1  # Windows
# source venv/bin/activate  # Mac/Linux

# Install dependencies
pip install fastapi uvicorn pydantic anthropic python-dotenv

# Add your Anthropic API key (required for /explain only)
echo ANTHROPIC_API_KEY=your-key-here > .env

# Start the server
uvicorn main:app --reload
```

Open [http://localhost:8000/docs](http://localhost:8000/docs) to explore the API.

Sample request payloads are included in `POST1-recommend.txt`, `POST2-explain.txt`, and `POST3-scenario.txt`.

## Related Portfolio Work

This backend is part of a larger AI-assisted design project — a hypothetical Vanguard Sell & Rebalance Tool explored across UX research, hi-fi prototyping in Figma, and full-stack implementation.

Portfolio: [michaelcasey.figma.site](https://michaelcasey.figma.site)
