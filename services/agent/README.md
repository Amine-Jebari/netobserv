# Service: agent

## Responsibility

The AI brain of the platform. Subscribes to `network.anomalies`, retrieves relevant context from DynamoDB and pgvector, calls the Claude API to generate a remediation runbook, persists the runbook to MinIO and OpenSearch, and publishes a `network.runbooks` event.

## Processing pipeline per anomaly event

1. **Topology context** — query DynamoDB for the affected node and its peers
2. **RAG retrieval** — embed the anomaly description, query pgvector for top-k similar past runbooks
3. **LLM call** — send Claude a structured prompt with anomaly details + retrieved runbooks as few-shot examples
4. **Persist** — write the generated runbook Markdown to MinIO; index metadata in OpenSearch
5. **Publish** — emit a `network.runbooks` event for the API to surface to the frontend

## Runbook output format

Claude produces structured Markdown with sections: **Summary**, **Root Cause Analysis**, **Immediate Mitigation Steps**, **Long-term Recommendations**, **Verification Commands**.

## Planned tech

- Python 3.11 + `anthropic` SDK + `langchain-community`
- `pgvector` + `psycopg2` for vector similarity search
- `boto3` for MinIO/S3 and DynamoDB reads
- `aiokafka` consumer/producer

## Key env vars

`ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `POSTGRES_HOST`, `AGENT_RAG_TOP_K`, `DYNAMODB_ENDPOINT_URL`, `S3_ENDPOINT_URL`

## Health check

`GET /health` → `{"status": "ok", "runbooks_generated": <count>, "last_generated": "<ts>"}`
