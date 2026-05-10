# AI Homelab Infrastructure Assistant

## Project Overview

Build a local AI-powered infrastructure assistant for homelab monitoring, diagnostics, and infrastructure awareness.

The system will run entirely on self-hosted infrastructure inside Proxmox LXC containers and communicate through internal APIs.

The assistant should:
- monitor infrastructure state
- analyze logs
- retrieve historical incidents through RAG
- understand homelab topology
- execute approved diagnostic commands
- assist with troubleshooting using local LLMs

---

# Current Homelab Infrastructure

Loaded from current inventory. :contentReference[oaicite:0]{index=0}

## Infrastructure Services

| VMID | Name | IP | Purpose |
|---|---|---|---|
| 100 | adguard | 192.168.1.30 | DNS & Ad-blocking |
| 101 | nginxproxymanager | 192.168.1.40 | Reverse Proxy |
| 102 | nextcloudpi | 192.168.1.50 | Personal Cloud |
| 106 | n8n | 192.168.1.168 | Workflow Automation |
| 107 | ollama | 192.168.1.229 | LLM Inference |
| 109 | openwebui | 192.168.1.105 | AI Chat Interface |
| 110 | grafana | 192.168.1.95 | Visualization |
| 111 | loki | 192.168.1.174 | Log Aggregation |
| 112 | prometheus | 192.168.1.92 | Monitoring |
| 113 | qdrant | 192.168.1.239 | Vector Database |
| 114 | gitlab | 192.168.1.93 | Code Repository |

Stopped:
- jellyfin
- lmstudio

---

# Planned Architecture

## Existing Services

### Ollama
- **IP:** 192.168.1.229
- **Purpose:** Main LLM inference provider
- **Communication:** HTTP API

### Qdrant
- **IP:** 192.168.1.239
- **Purpose:** Vector database for RAG retrieval
- **Stores:**
  - Logs
  - Infrastructure snapshots
  - Service metadata
  - Historical troubleshooting data

### Loki
- **IP:** 192.168.1.174
- **Purpose:** Centralized log aggregation
- **Potential Integration:** Fetch logs directly into ingestion pipeline

### Prometheus
- **IP:** 192.168.1.92
- **Purpose:** Real-time metrics and infrastructure monitoring
- **Metrics Tracked:**
  - CPU usage
  - RAM usage
  - Network metrics
  - Container statistics

### Grafana
- **IP:** 192.168.1.95
- **Purpose:** Visualization and optional dashboard integration

### GitLab
- **IP:** 192.168.1.93
- **Purpose:** Repository hosting and CI/CD
- **Note:** Not intended to host the application runtime

## New Service To Build

### AI Stack Container

**Recommendation:** Create new LXC container named `ai-stack`

**Responsibilities:**
- Orchestration
- FastAPI backend
- Frontend backend
- RAG orchestration
- Infrastructure scanner
- Command approval system
- Retrieval pipeline
- Prompt assembly

## Core Features

### 1. Infrastructure Discovery

Implement automatic discovery using Proxmox API.

**The system should:**
- Detect new LXCs/VMs
- Detect config changes
- Update infrastructure knowledge automatically

**Store:**
- Hostname
- IP address
- Services
- Ports
- Runtime state
- Network config
- Mounted storage

**Example knowledge object:**
```json
{
  "hostname": "nextcloudpi",
  "ip": "192.168.1.50",
  "services": ["nginx", "php"],
  "role": "Personal Cloud"
}
```

### 2. Log Ingestion Pipeline

**Data Sources:**
- Loki
- journalctl
- syslog
- Proxmox logs

**Pipeline:**
- Fetch recent logs
- Chunk logs
- Generate embeddings
- Upsert into Qdrant

**Requirements:**
- Automatic ingestion
- Incremental updates
- Avoid duplicate embeddings
### 3. Live Infrastructure Context

**Important:** Real-time data must NOT come from RAG.

**Fetch live data directly from:**
- Proxmox API
- Prometheus
- SSH commands

**Examples:**
- CPU usage
- RAM usage
- Disk usage
- Network state
- VM status

Inject this directly into prompts.

### 4. RAG Retrieval

**Use Qdrant to retrieve:**
- Historical incidents
- Previous errors
- Infrastructure snapshots
- Semantic log matches

**Questions the assistant should answer:**
- "Why is Nextcloud slow?"
- "Which container runs DNS?"
- "Show recent nginx errors"
- "Where is Ollama hosted?"
### 5. Safe Command Execution

**Critical:** The LLM must NEVER execute shell commands directly.

**Execution Flow:**
```
LLM → Structured Action → Backend Validation → Approval → Execution
```

**Structured Action Example:**
```json
{
  "action": "tail_logs",
  "service": "nginx",
  "lines": 50
}
```

Backend converts actions into safe commands.

**Allowed Diagnostic Commands:**
- `ip`, `ss`, `find`, `ls`, `grep`, `cat`, `tail`
- `df`, `free`, `ps`, `journalctl`, `systemctl`

**Blocked Commands:**
- `rm`, `sudo`, `shutdown`, `reboot`, `chmod`, `chown`, `apt`

**Requirements:**
- `shell=False`
- Strict validation
- Argument sanitization
### 6. Approval Workflow

**Before execution:**
- Show command preview
- Show target container
- Require manual approval

**UI must include:**
- Approve button
- Deny button
- Execution output
- AI explanation
### 7. Multi-Model Support

**Supported Providers:**
- Ollama
- BitNet.cpp

**Provider abstraction example:**
```python
generate(provider="ollama", prompt=...)
generate(provider="bitnet", prompt=...)
```

Allow runtime switching between models.

### 8. Web Interface

**Build:**
- Infrastructure dashboard
- AI chat interface
- Command approval UI
- Live logs view
- Infrastructure inventory
- Model selection

**Frontend Options:**
- React
- Streamlit

**Preferred Stack:**
- React frontend + FastAPI backend
## API Design

### Suggested Routes

**POST Endpoints:**
- `POST /chat` - Send chat queries to the assistant
- `POST /execute` - Execute approved commands
- `POST /approve` - Approve pending command execution
- `POST /scan` - Trigger infrastructure scan
- `POST /ingest` - Ingest logs and infrastructure data

**GET Endpoints:**
- `GET /containers` - List all containers
- `GET /infrastructure` - Get infrastructure overview
- `GET /metrics` - Retrieve real-time metrics
- `GET /logs` - Fetch logs
## Project Structure

```
ai-stack/
├── backend/
│   ├── api/
│   ├── rag/
│   ├── ingestion/
│   ├── execution/
│   ├── proxmox/
│   ├── llm/
│   ├── scanner/
│   └── prompts/
├── frontend/
├── config/
└── docker/
```
## Prompting Rules

### System Behavior
- Infrastructure-focused
- Concise
- Safe
- Deterministic
- JSON-first tool usage

### Never
- Hallucinate infrastructure state
- Generate destructive commands
- Execute actions automatically

### The Assistant Should
- Analyze
- Explain
- Diagnose
- Retrieve
- Recommend safe actions
## Development Order

1. Create ai-stack LXC
2. Connect to Proxmox API
3. Build infrastructure scanner
4. Integrate Qdrant
5. Implement log ingestion
6. Integrate Ollama
7. Add retrieval pipeline
8. Build execution layer
9. Add approval system
10. Build frontend
11. Add BitNet provider
12. Improve prompts
## Final Goal

The final system should function as a local AI-powered DevOps assistant capable of:

- Infrastructure awareness
- Semantic retrieval
- Real-time diagnostics
- Safe command execution
- Automated infrastructure discovery
- Local LLM inference
- Modular microservice orchestration