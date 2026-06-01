# Social_DMs

## Automated Community Management Architecture

Social_DMs is a powerful, hybrid agentic framework engineered to solve communication bottlenecks for maintainers, community managers, and researchers. By integrating directly with the Meta Graph API, the system provides a continuous, automated pipeline for archiving, semantic analysis, and intelligent routing of high-volume inbound messages.

Unlike standard SaaS platforms, Social_DMs is built for flexibility, data sovereignty, and the "vibecoding" philosophy—allowing maintainers to focus on high-level architectural oversight rather than line-by-line manual interactions. It leverages a hybrid inference model, allowing operations to be routed through cloud LLM APIs (e.g., OpenAI) or processed locally on edge hardware, from a Raspberry Pi 4 handling routine classifications to an RTX 3070 or Apple M-series silicon running heavier local models.

## Core Features

*   **Meta Graph API Integration:** Seamlessly connects to Instagram and Meta services via secure webhooks for real-time message ingestion.
*   **Hybrid Agentic Routing:** Intelligently triages inbound communications. High-complexity queries can be routed to powerful cloud LLMs, while routine classification and summarization run on local hardware.
*   **Semantic Vector Storage:** Ingests conversations into a vector database, enabling RAG (Retrieval-Augmented Generation) for highly contextualized agent responses and historical community insights.
*   **Secure Tunneling Architecture:** Natively supports Cloudflare Tunnels to safely expose local webhook endpoints without exposing the host network.

## Prerequisites

*   Python 3.10+
*   Meta Developer Account (with Graph API access and a configured App)
*   Cloudflare `cloudflared` installed for secure tunnel creation
*   A local Vector Database instance (e.g., ChromaDB, Milvus, or Qdrant)
*   Optional: Local LLM runtime (Ollama) configured for edge inference

## Installation & Setup

**1. Clone the repository:**
```bash
git clone [https://github.com/yourusername/Social_DMs.git](https://github.com/yourusername/Social_DMs.git)
cd Social_DMs
