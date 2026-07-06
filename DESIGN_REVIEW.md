# Funkcionalni & dizajn pregled — AI Homelab Assistant

**Obseg:** pravilnost metod upravljanja in pomoči proti uradnim dokumentacijam
(Proxmox VE API, Ollama, Qdrant, Loki) ter zmogljivost/dizajn integracij.
Varnost je pokril prejšnji pregled (`CODE_REVIEW.md`) in se tu ne ponavlja.

**Zaključek:** integracije so v osnovi pravilne — poti, avtentikacija in
formati ustrezajo uradnim API-jem. Glavne pomanjkljivosti so bile v
*zmogljivosti* (N+1 sken), *manjkajočih zmožnostih* (runtime IP za DHCP,
multi-node izvajanje) in nekaj funkcionalnih hroščih (SSH deadlock, zamešan
SSH/API uporabnik, prepis odobrenega ukaza). Vse je popravljeno na tej veji;
`pct/qm start/stop` zdaj tečeta prek Proxmox API-ja namesto SSH.

---

## 1. Preverjeno proti uradnim dokumentacijam ✅

### Proxmox VE
- Vse uporabljene poti (`/nodes`, `/nodes/{n}/lxc|qemu`, `/{vmid}/config`,
  `/{vmid}/status/current`, `/version`) so pravilne.
- Avtentikacija `PVEAPIToken=user@realm!tokenid=secret` je točen format
  API žetona; API je vedno HTTPS na 8006 (`verify_ssl` vpliva samo na
  validacijo certifikata) — oboje pravilno.
- Navodilo modelu, naj z `pct`/`qm` uporablja številčni vmid, je pravilno.

### Ollama
- Nativni `/api/chat` z `options.num_ctx`/`temperature` na zahtevo: pravilna
  izbira (OpenAI-kompatibilni endpoint `options` ne podpira).
- Format tool-result sporočila `{"role": "tool", "tool_name": ..., "content": ...}`
  je točno po dokumentaciji.
- `/api/embed` z `input` kot seznamom in odgovorom v polju `embeddings`: pravilno.
- **Popravek prejšnjega pregleda:** `CODE_REVIEW.md` §3 trdi, da je
  `gemma4:e4b` tipkarska napaka. To ne drži več — Gemma 4 (E2B/E4B) je izšla
  in je na voljo na Ollama (`ollama pull gemma4:e4b`) z **nativnim tool
  callingom**. Konfiguracija je torej pravilna; besedilni razčlenjevalniki
  tool klicev v `ollama/client.py` ostajajo koristni le za starejše modele.

### Loki
- `query_range` z nanosekundnima `start`/`end` in `limit` je pravilen;
  privzeti `direction=backward` vrne najnovejše vrstice, kar je tu želeno.

### Qdrant
- `upsert`, `scroll`, `retrieve`, filtri: pravilno.
- `client.search()` je bil v novejših izdajah qdrant-client **odstranjen**;
  ker `requirements.txt` pina samo `>=1.10.0`, bi svež install podrl
  semantično iskanje. → Migrirano na `query_points` (glej §3).

---

## 2. Ugotovitve

### 🔴 D1 — N+1 sken inventarja (popravljeno)
`scan_inventory` je za N gostov naredil `2 + 2N` HTTP klicev (seznam nodov,
per-node seznama, per-gost `config` + `status/current`). Proxmox ima za to
namenski endpoint: **`GET /cluster/resources`** vrne vse goste celotne gruče
(vmid, ime, status, node, tip) v enem klicu; per-gost `status/current` je bil
povsem odveč, ker je status že v seznamu. Novi sken: `1 + N` klicev (config
ostane zaradi hostname/statičnega IP), template gosti se preskočijo.

### 🔴 D2 — SSH deadlock pri velikem izpisu (popravljeno)
`execute` je klical `recv_exit_status()` **pred** branjem izhoda. Ko izpis
preseže SSH okno (~2 MB, npr. `journalctl` brez omejitve ali `cat` velike
datoteke), se oddaljeni proces blokira pri pisanju, lokalno pa čakamo na
izhodno kodo → obojestransko čakanje do timeouta, izpis izgubljen. Zdaj se
oba tokova praznita v zanki do `exit_status_ready()`, z dejanskim uveljavljanjem
`timeout` roka.

### 🔴 D3 — SSH prijava z API identiteto (popravljeno)
SSH uporabnik je bil izpeljan iz `PROXMOX_USER` (identiteta API žetona, npr.
`ai-stack`), ki praviloma **ni** Unix račun na vozlišču — izvajanje je delovalo
samo, če je bil `PROXMOX_USER=root@pam`, kar pa sili API žeton na roota. Nova
nastavitev `PROXMOX_SSH_USER` loči obe identiteti; brez nje ostane staro
obnašanje z opozorilom v logu.

### 🔴 D4 — `/execute` je lahko izvedel drug ukaz od odobrenega (popravljeno)
Če je klient v `POST /execute` poslal `command`, se je izvedel ta namesto
ukaza iz odobritve (allowlist je sicer še veljal, a odobritev ni več vezala
tega, kar se dejansko požene). Zdaj se izvede izključno ukaz iz odobritve;
drugačen `command` v zahtevi vrne 400 in sprosti claim.

### 🟠 D5 — start/stop prek SSH namesto API (popravljeno — nova pot)
`pct/qm start|stop` prek SSH ima dve sistemski slabosti: (1) SSH vedno cilja
en host, `pct`/`qm` pa morata teči na vozlišču, kjer gost živi — na več-vozliščni
gruči ukaz za goste na drugih vozliščih ne uspe; (2) zahteva root lupino za
nekaj, kar API omogoča z drobnozrnatim privilegijem. Zdaj gredo te akcije
prek `POST /nodes/{node}/{lxc|qemu}/{vmid}/status/{start|stop}`: vozlišče se
najde prek `cluster/resources`, klic vrne task **UPID**, ki se polla na
`GET /nodes/{node}/tasks/{upid}/status` do `status=="stopped"`; uspeh je
`exitstatus=="OK"`. Potreben privilegij: **VM.PowerMgmt**. Tok odobritev
ostaja nespremenjen — spremenil se je samo izvajalni mehanizem.

### 🟠 D6 — IP se pri DHCP gostih ni zaznal (popravljeno)
`_extract_ip` bere samo statični `ip=` iz configa; pri `ip=dhcp` (najpogostejši
homelab primer) je bil IP vedno prazen. Dodan runtime fallback za tekoče
goste: LXC prek `GET /nodes/{n}/lxc/{vmid}/interfaces`, QEMU prek guest-agenta
(`.../agent/network-get-interfaces`). Oboje best-effort — starejši PVE ali VM
brez agenta tiho vrne prazno.

### 🟠 D7 — hardkodiran rezervni SSH IP (popravljeno)
Brez nastavljenega hosta se je SSH tiho povezal na vgrajeni `192.168.1.147`.
Zdaj se host izpelje iz API URL-ja, sicer se vrže jasna napaka.

### 🟠 D8 — qdrant-client `search()` (popravljeno)
Glej §1/Qdrant. `LogStore.search_logs` zdaj uporablja `query_points`.

### 🟡 D9 — QEMU `hostname` (popravljeno)
QEMU config nima ključa `hostname` (to je LXC posebnost); za VM-je se zdaj
kot hostname uporabi config `name`.

### 🟡 Manjše opombe (brez sprememb kode)
- System prompt je skoraj identično podvojen v `/chat` in `/chat/stream` —
  ob spremembi je treba posodobiti oba.
- `/debug/proxmox` vrača poln traceback; pusti samo za razvoj.
- Zgodovina skenov in "recent logs" se sortirata client-side znotraj omejenega
  scroll okna (256 / 1024 točk) — nad tem oknom je "najnovejše" best-effort.
- `ExecuteRequest.timeout` nima zgornje meje (`ge=1` brez `le`).

---

## 3. Spremembe na tej veji

- `backend/proxmox/client.py` — sken prek `cluster/resources` (1+N klicev,
  preskok template), `get_runtime_ip` (LXC interfaces / QEMU guest agent),
  `start_guest`/`stop_guest`/`get_task_status`/`wait_for_task`, `find_guest`.
- `backend/execution/service.py` — `pct/qm start|stop` → Proxmox API s task
  pollingom; drain zanka namesto deadlock vzorca; `PROXMOX_SSH_USER`;
  odstranjen hardkodiran IP (izpelji iz API URL ali napaka).
- `backend/api/routes.py` — `/execute` veže izvajanje na odobreni ukaz (400 ob
  neujemanju); `proxmox_ssh_user` v settings endpointih.
- `backend/qdrant/logs.py` — `search()` → `query_points()`.
- `backend/config/settings.py`, `backend/api/models.py` — `proxmox_ssh_user`.
- `.env.example`, `README.md` — `PROXMOX_SSH_USER`, potrebni privilegiji žetona.
- `tests/test_proxmox_client.py` (novo) + razširjen `tests/test_execution_flow.py`
  — 20 testov, vsi zeleni.

## 4. Nastavitev za živo rabo

1. API žetonu dodeli vlogo z **Sys.Audit + VM.Audit** (na `/`) in
   **VM.PowerMgmt** (na `/vms` ali posameznih gostih).
2. V `.env` nastavi `PROXMOX_SSH_USER=root` (ali drug Unix račun) — SSH se zdaj
   uporablja samo še za diagnostične ukaze, ne za start/stop.
3. Preverba v živo:
   - `curl -s localhost:8000/scan | jq .container_count` — en hiter sken
     (opazno hitrejši kot prej pri več gostih).
   - v UI odobri predlog `pct start <vmid>` → v izpisu dobiš `UPID` in
     `exitstatus: OK`; task je viden tudi v PVE UI (Tasks).
   - gost z DHCP: po skenu ima izpolnjen IP (LXC takoj, QEMU ob nameščenem
     guest agentu).
