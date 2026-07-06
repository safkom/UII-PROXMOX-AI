# Code & Design Review — AI Homelab Assistant (UII-PROXMOX-AI)

**Scope:** management/help methods and their correctness against the real
Proxmox VE API, plus security and design of the FastAPI backend, SSH execution
layer, approval workflow, and frontend.

**Verdict:** The project is well structured and the Proxmox integration is
implemented correctly. The blocking issues were in *safety* — an allowlist
bypass, a missing auth option, and an execution race — not in how Proxmox is
driven. The critical items have been fixed on this branch; a few hardening items
are left as opt-in recommendations so existing setups keep working.

---

## 1. Proxmox VE API usage — correct ✅

All Proxmox interactions match the real PVE REST API. Inventory is **read-only**
via the API; any state change goes through the SSH + approval path, which is the
right boundary.

| Method | Path | Purpose | Correct? |
|--------|------|---------|----------|
| GET | `/api2/json/nodes` | list nodes | ✅ |
| GET | `/api2/json/nodes/{node}/lxc` | list LXC containers | ✅ |
| GET | `/api2/json/nodes/{node}/qemu` | list QEMU VMs | ✅ |
| GET | `/api2/json/nodes/{node}/{lxc\|qemu}/{vmid}/config` | guest config | ✅ |
| GET | `/api2/json/nodes/{node}/{lxc\|qemu}/{vmid}/status/current` | live status | ✅ |
| GET | `/api2/json/version` | health probe | ✅ |

- **Auth header** `PVEAPIToken=user@realm!tokenid=secret`
  (`backend/config/settings.py:56-58`) is exactly the PVE API token format.
- **Base URL** is always `https://…:8006`
  (`settings.py:60-79`) — correct, PVE serves the API over HTTPS only;
  `verify_ssl` controls certificate validation, not the scheme.
- The chat system prompt instructs the model to use **numeric vmid** with
  `pct`/`qm` and never the name (`backend/api/routes.py`) — this matches how
  those CLIs actually work.
- `_extract_ip` reads `net*` / `ipconfig*` keys looking for `ip=` — a reasonable
  heuristic for both LXC and cloud-init VMs.

Minor note: `get_container_config(..., "qemu")` then reads `hostname`, which is
an LXC-only config key, so it is always empty for VMs. Harmless (dead lookup).

---

## 2. Findings and fixes

Severity order. "Fixed" items are changed on this branch; "Recommendation" items
are opt-in and default to the previous behavior.

### 🔴 F1 — `find` allowlist bypass (fixed)
`find` was allow-listed with no restriction, so
`find / -delete` and `find / -name x -exec rm -rf {} +` passed validation
cleanly: no shell metacharacters are involved, and the `BLOCKED` set only
inspects `argv[0]`, so the nested `rm` was never checked. This fully defeated the
command allowlist — the single most serious issue.

**Fix** (`backend/execution/service.py`): added a per-command dangerous-flag
denylist enforced in `validate()`. For `find`, the flags
`-exec, -execdir, -ok, -okdir, -delete, -fprint, -fprintf, -fprint0, -fls`
are rejected regardless of position, while ordinary read-only searches
(`find /var/log -name '*.log'`) still work.

### 🔴 F2 — No authentication on any endpoint (fixed: opt-in auth added)
Every route was open, while the app binds to `0.0.0.0`. `POST /execute` runs
root SSH commands; `PATCH /approvals/{id}` approves; `PATCH /settings` rewrites
`.env` secrets on disk. The README documents "no auth" as an intentional
trusted-LAN choice, but there was no way to lock it down.

**Fix**: added an **optional** API token (`backend/api/security.py`,
`API_AUTH_TOKEN` in settings). When set, every API request must present
`Authorization: Bearer <token>` or `X-API-Key: <token>`; the check uses a
constant-time comparison. When unset, behavior is unchanged (trusted-LAN mode).
The dependency is applied to the whole router in `backend/api/main.py`, and the
frontend attaches the header when a token is entered in Settings (stored in
`localStorage`, never sent to the model). The `/ui` static mount stays open so
the page can load and the operator can enter the token.

### 🔴 F3 — Approval double-execution (TOCTOU) (fixed)
`execute_command` did `get()` → status check → `execute()` → `mark_executed()`
non-atomically, and `mark_executed` didn't verify the current status. Two
concurrent `POST /execute` with the same `approval_id` both read `"approved"`
and ran the approved command **twice**.

**Fix**: added `ApprovalStore.claim_for_execution`, which atomically does
`UPDATE … SET status='executing' WHERE id=? AND status='approved'` and only
succeeds if `rowcount == 1`. The route now claims first; the loser gets `409`.
`mark_executed` only transitions from `executing`, and a new `release_claim`
reverts to `approved` if execution errors out so a transient failure can't
strand the record.

### 🟠 F4 — No HTTP timeout on Proxmox requests (fixed)
`ProxmoxClient._request` set no timeout, so a hung/unreachable PVE host blocked
the worker thread indefinitely.

**Fix** (`backend/proxmox/client.py`): a default `(connect, read)` timeout of
`(5, PROXMOX_REQUEST_TIMEOUT)` (default 15s), applied via `setdefault` so callers
can still override.

### 🟠 F5 — SSH host-key verification disabled (fixed: configurable)
`_get_ssh_client` used `AutoAddPolicy`, blindly trusting any host key (MITM
exposure), with root password auth.

**Fix**: added `PROXMOX_SSH_STRICT_HOST_KEY`. When true, the client
`load_system_host_keys()` and uses `RejectPolicy`; when false (default) it keeps
the previous auto-add behavior but logs a warning. **Recommendation:** enable
strict mode and pre-populate `~/.ssh/known_hosts`; consider key-based auth
instead of a root password.

### 🟡 F6 — TLS certificate validation off by default (recommendation)
`proxmox_verify_ssl` defaults to `False`. Left as-is to avoid breaking
self-signed homelab certs, but `.env.example` now recommends `true` with a valid
or pinned certificate.

---

## 3. Non-blocking notes (no code change)

- **`OLLAMA_MODEL=gemma4:e4b` is almost certainly a typo** — Gemma releases are
  `gemma2` / `gemma3` (e.g. `gemma3:4b`). The model won't pull as written. Pick a
  model you actually have installed on the Ollama node.
- **Qdrant ordering**: history and recent-logs queries `scroll` up to a fixed
  window and sort by timestamp client-side; entries beyond the window are
  silently dropped. Fine for a homelab, but "recent" is best-effort past the
  window size.
- **State-changing allowlist entries**: `systemctl start/stop/restart` and
  `pct/qm start/stop` are intentionally permitted. Reasonable for a diagnostics
  assistant, but only safe once F2 (auth) is enabled if the port is reachable
  beyond a trusted host.
- **Info disclosure**: `cat`/`journalctl`/`grep` are unrestricted, so an approved
  command can read anything root can (e.g. `/etc/shadow`). Expected for a
  root-level diagnostics tool; keep approvals meaningful.
- **XSS**: LLM/log text is escaped via `escapeHtml` before every limited-markdown
  `innerHTML` render, so injection is mitigated — but the safety hinges entirely
  on `escapeHtml` running before each `innerHTML` assignment. Keep that
  invariant if you extend the renderer.

---

## 4. How to run it safely

1. **Set `API_AUTH_TOKEN`** to a long random value in `.env`, and enter the same
   token in the UI Settings → API Access field. Without this, anyone who can
   reach the port can run root commands.
2. **Do not expose the port beyond the LAN.** Even with a token, put it behind a
   reverse proxy / VPN; the assistant executes as root on your Proxmox node.
3. **Set `PROXMOX_VERIFY_SSL=true`** with a valid or pinned certificate.
4. **Set `PROXMOX_SSH_STRICT_HOST_KEY=true`** and populate `known_hosts`;
   prefer SSH key auth over the root password.
5. Fix `OLLAMA_MODEL` to a model that actually exists on your Ollama host.

---

## 5. Changes in this branch

- `backend/execution/service.py` — `find` dangerous-flag denylist; configurable SSH host-key policy.
- `backend/approvals/store.py` — atomic `claim_for_execution`, `release_claim`; `mark_executed` guarded by `executing` status.
- `backend/api/routes.py` — atomic claim + `409` semantics in `/execute`.
- `backend/api/security.py` (new) + `backend/api/main.py` — optional API-token auth.
- `backend/proxmox/client.py` — default request timeout.
- `backend/config/settings.py` — `api_auth_token`, `proxmox_request_timeout`, `proxmox_ssh_strict_host_key`.
- `.env.example` — new vars + hardening guidance.
- `frontend/index.html`, `frontend/app.js` — API token field + auth header on requests.
- `tests/test_execution_flow.py` — tests for the `find` bypass, the claim/double-execution guard, and optional auth.

All tests pass: `python -m pytest tests/ -q` → 12 passed.
