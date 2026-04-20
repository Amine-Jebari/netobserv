# Network Observability Platform

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-AWS-7B42BC?logo=terraform&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

A production-grade network observability platform that streams live routing telemetry, detects anomalies with machine learning, and deploys AI agents to automatically generate remediation runbooks — all visualized in a real-time React dashboard.

---

## Why This Project

Modern networks generate millions of telemetry events per second. Human operators cannot monitor, correlate, and respond to routing anomalies at that speed. This platform closes the gap:

- **Real-time ingestion** via Kafka-compatible streaming (Redpanda locally, Kinesis on AWS)
- **ML-based anomaly detection** using Isolation Forest (PyOD), trained on a rolling baseline
- **AI-powered remediation** via an LLM agent (Claude API) that queries a RAG knowledge base of past runbooks, then writes a new runbook specific to the detected anomaly
- **Live topology visualization** rendered with D3.js, showing affected nodes, edges, and BGP path changes in real time
- **Dual-target architecture** — the exact same application code runs locally in Docker Compose or deploys to AWS with only environment variables changing

This is intentionally portfolio-grade: clean code, typed, linted, containerized, and infrastructure-as-code from day one.

---

## Architecture

```
See ARCHITECTURE.md for the full design document and ASCII diagram.
```

![Architecture Diagram](docs/architecture-diagram.png)

> *Diagram coming in Phase 2.*

---

## Quick Start

> Prerequisites: Docker Desktop 24+, `make`, Python 3.11+

```bash
# 1. Clone and enter the repo
git clone https://github.com/<your-handle>/netobserv.git
cd netobserv

# 2. Copy environment config and fill in your API key
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY

# 3. Start the full stack
make up

# 4. Seed the runbook knowledge base
make seed

# 5. Open the dashboard
open http://localhost:3000
```

Service endpoints after `make up`:

| Service | URL |
|---|---|
| React Dashboard | http://localhost:3000 |
| FastAPI Backend | http://localhost:8000/docs |
| Grafana | http://localhost:3001 |
| InfluxDB UI | http://localhost:8086 |
| OpenSearch Dashboards | http://localhost:5601 |
| Redpanda Console | http://localhost:8080 |

---

## Project Structure

```
netobserv/
├── services/
│   ├── simulator/    Network telemetry generator
│   ├── collector/    Kafka → storage fanout
│   ├── topology/     Live graph builder
│   ├── anomaly/      PyOD anomaly detection engine
│   ├── agent/        LangChain AI agent + RAG
│   ├── api/          FastAPI backend
│   └── frontend/     React + D3.js dashboard
├── terraform/        AWS infrastructure (mirrors Docker Compose stack)
├── grafana/          Dashboard provisioning
├── scripts/          Seed and training scripts
└── docs/             Architecture docs and diagrams
```

---

## Deploy to AWS

All AWS infrastructure is codified in Terraform under `terraform/`. The local Docker Compose stack and the AWS stack use **identical application code** — only environment variables differ.

```bash
# Authenticate with AWS
aws configure

# Preview infrastructure changes
make tf-plan

# Apply (creates all resources in your AWS account)
make tf-apply
```

AWS services used: Kinesis Data Streams, Timestream, OpenSearch Service, DynamoDB, S3, RDS (Postgres + pgvector), SageMaker Serverless Inference, ECS Fargate, API Gateway, CloudFront.

> **Cost note:** Running the full AWS stack incurs charges. Use `make tf-destroy` to tear down all resources when not in use.

---

## Development

```bash
make test       # Run pytest across all services
make lint       # Run ruff + mypy
make logs       # Tail all container logs
make down       # Stop the stack
make clean      # Stop and remove all volumes (destructive)
```

---

