# AI Homelab Assistant (ai-stack)

Samogostovan AI DevOps asistent za Proxmox homelab. FastAPI zaledje povezuje
lokalni LLM (Ollama), vektorsko bazo (Qdrant), agregacijo dnevnikov (Loki) in
Proxmox API za lastnim spletnim vmesnikom, osredotočenim na klepet. Asistent
zna pregledovati infrastrukturo, semantično iskati po dnevnikih in predlagati
diagnostične ukaze, ki se izvedejo šele po izrecni človeški odobritvi.

## Funkcionalnosti

- **Integracija lokalnega LLM (Ollama)** — nativno klicanje orodij prek
  `/api/chat` z rezervnimi razčlenjevalniki za modele, ki orodja kličejo v
  besedilni obliki (Gemma, Llama, Mistral). Model je mogoče zamenjati med
  delovanjem kar iz vmesnika.
- **RAG nad dnevniki in infrastrukturo** — dnevniki iz Lokija in posnetki
  infrastrukture se vektorizirajo z lokalnim modelom za vložitve
  (`nomic-embed-text` prek Ollame) in shranijo v Qdrant. LLM lahko med
  odgovarjanjem pokliče orodje `search_logs` in pridobi semantično ustrezne
  vrstice dnevnikov.
- **Odkrivanje infrastrukture** — pregleda vsa Proxmox vozlišča (LXC vsebniki
  in QEMU virtualke: status, IP, ime gostitelja) ter v Qdrantu hrani trenutno
  stanje in zgodovino posnetkov.
- **Varno izvajanje ukazov** — LLM nikoli ne izvaja ničesar neposredno.
  Predlaga strukturirano akcijo; zaledje jo preveri proti seznamu dovoljenih
  ukazov (vključno s pravili za podukaze `pct`/`qm`/`pvesh`/`systemctl` in
  prepovedjo posebnih znakov lupine); uporabnik jo v vmesniku odobri ali
  zavrne; šele nato se ukaz izvede, odobritev pa se označi kot izvedena, da je
  ni mogoče pognati znova. Akcije `pct/qm start/stop` gredo prek Proxmox API
  (asinhroni task z UPID, deluje na katerem koli vozlišču gruče, zahteva
  privilegij `VM.PowerMgmt` na žetonu); ostali diagnostični ukazi se izvedejo
  prek SSH na Proxmox vozlišču (`PROXMOX_SSH_USER`).
- **Lasten spletni vmesnik** — ročno napisana enostranska aplikacija v čistem
  JS/CSS: klepet (pretakanje odgovorov, vidni klici orodij in razmislek),
  inventar vsebnikov, brskalnik dnevnikov s semantičnim iskanjem, čakalna
  vrsta odobritev in plošča z nastavitvami. Pisave so gostovane lokalno, zato
  vmesnik deluje tudi v omrežju brez dostopa do interneta.

## Zagon

```bash
python -m pip install -r requirements.txt
uvicorn backend.api.main:app --reload --host 0.0.0.0 --port 8000
```

Nato v brskalniku odpri `http://127.0.0.1:8000/ui`.

Potrebna modela na Ollama strežniku:

```bash
ollama pull gemma4:e4b          # model za klepet (nastavljiv prek OLLAMA_MODEL)
ollama pull nomic-embed-text    # model za vložitve za RAG (OLLAMA_EMBED_MODEL)
```

Kopiraj `.env.example` v `.env` ter vpiši svoje naslove storitev in skrivnost
Proxmox API žetona. Žeton potrebuje privilegije `Sys.Audit` in `VM.Audit`
(inventar) ter `VM.PowerMgmt` (zagon/zaustavitev gostov prek API-ja). Če
zaledje teče lokalno in se povezuje na Proxmox na drugem računalniku, nastavi
`PROXMOX_HOST_IP` in `PROXMOX_PORT`; za SSH izvajanje nastavi še
`PROXMOX_SSH_USER` (Unix račun na vozlišču, običajno `root`). `OLLAMA_NUM_CTX`
omejuje kontekstno okno na zahtevo — na šibkejših grafičnih karticah naj
ostane zmeren (4096).

Teste poženeš z:

```bash
python -m pytest tests/
```

## Arhitektura

```
frontend (enostranska aplikacija v čistem JS, strežena na /ui)
        │ REST + NDJSON pretakanje
backend (FastAPI)
 ├── ollama/      LLM odjemalec (zanka klicanja orodij) + vložitve
 ├── proxmox/     Proxmox API odjemalec (pregled inventarja)
 ├── loki/        pridobivanje dnevnikov (LogQL)
 ├── qdrant/      vektorski shrambi: dnevniki + posnetki infrastrukture
 ├── approvals/   SQLite čakalna vrsta odobritev
 └── execution/   preverjeno izvajanje ukazov prek SSH
```

## Varnostni model (znane omejitve)

To je orodje za homelab v zaupanja vrednem lokalnem omrežju: API sam **nima
avtentikacije**. Vgrajene zaščite: izvajanje ukazov zahteva zapis odobritve,
ukazi se preverjajo proti strogemu seznamu dovoljenih (program + podukaz),
posebni znaki lupine so zavrnjeni, skrivnosti so v nastavitvenem API-ju samo
zapisljive (write-only), izvajalnik pa se povezuje prek SSH s poverilnicami v
`.env` (nikoli v repozitoriju). Vrat ne izpostavljaj izven lokalnega omrežja
brez dodane avtentikacije in TLS.
