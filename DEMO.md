# EI-ADS — Local Demo Guide

This is everything you need to **run the EI Anomaly Detection Solution locally
and demonstrate it end to end** — submit an EI claim as a claimant, watch the
detection pipeline score it, then review it as an investigator. No cloud, no
logins, no passwords.

The whole stack runs on your machine in containers (Podman). One command starts
it; the demo is entirely in the browser.

---

## 1. Prerequisites (one-time setup)

You need a Mac (Apple Silicon or Intel) with:

1. **Podman Desktop** (or Podman CLI) + **podman-compose**.
   - Install Podman Desktop from https://podman-desktop.io, or via Homebrew:
     `brew install podman podman-compose`
2. **A Podman machine with enough resources.** The stack runs ~29 containers
   including databases and ML services, so give the VM room:
   ```zsh
   export CONTAINERS_MACHINE_PROVIDER=applehv
   podman machine init --cpus 4 --memory 12288 --disk-size 150   # first time only
   podman machine start
   ```
   > If you already have a machine, bump its memory once so builds don't run out
   > of RAM: `podman machine stop && podman machine set --memory 12288 && podman machine start`.
3. **Python 3.11 dev venv** (used only to mint the local dev security token — no
   internet identity provider is needed locally). From the repo root:
   ```zsh
   python3.11 -m venv .venv-core
   .venv-core/bin/pip install cryptography python-jose
   ```
   (If `.venv-core` already exists, skip this.)

> First run downloads container base images and builds all services — budget
> **20–40 minutes** on a fresh machine. Subsequent runs are much faster.

---

## 2. Start it — one command

From the repository root:

```zsh
scripts/dev/start-stack.sh
```

That's it. The script:
- starts the Podman machine,
- mints the local dev security token (only if missing or expired),
- builds every service image,
- starts the full stack,
- warms up the detection engines,
- and prints the demo URL + steps when it's ready.

When it finishes you'll see a banner ending with **`http://localhost:8090`**.

Already built once and just want to start faster? Skip the rebuild:
```zsh
scripts/dev/start-stack.sh --no-build
```

---

## 3. Run the demo (in the browser)

Open **http://localhost:8090**.

There is no real login screen locally, so the app shows a small **"🔧 Demo as"**
switcher in the top-right header. You use it to act as either persona with one
click — no cookies, tokens, or DevTools.

### As a Claimant — submit a claim
1. Click **🔧 Demo as: Claimant** in the header.
2. Click **Submit a claim** in the nav.
3. Fill the form with these known-good values:

   | Field                | Value              |
   |----------------------|--------------------|
   | SIN                  | `193456787`        |
   | Employer BN          | `123456789RP0001`  |
   | Insurable hours      | `800`              |
   | Insurable earnings   | `12000`            |
   | Separation date      | `2024-03-15`       |
   | Reason               | `Shortage of work` |

4. Click **Submit**. You'll see a confirmation with a **Claim ID**.

Behind the scenes the claim now flows through the pipeline: it's paired with a
matching Record of Employment, all six detection engines analyze it
concurrently, an ensemble model scores it, and the result is stored.

### As an Investigator — review the claim
5. Click **🔧 Demo as: Investigator** in the header.
6. Click **Dashboard** in the nav.
7. Your claim appears in the review queue with its **risk score and decision**.
   Click it to see the per-engine indicators, the SHAP contribution chart, and
   the bilingual explanation, and to record a disposition.

That's the full loop: **claimant submits → pipeline scores → investigator
reviews**, all from the browser.

---

## 4. Good to know

- **"My claim isn't in the dashboard."** The investigator queue only shows
  claims that were **flagged** (FLAG_FOR_REVIEW or INVESTIGATE). A low-risk
  claim scores **AUTO_APPROVE** and is intentionally hidden. This usually
  happens if you submit within the first few seconds after start-up, before the
  ML engines finish warming. **Just submit one more claim** — it will score
  higher and appear.
- **Personas are separate on purpose.** A claimant sees only their own claim; an
  investigator sees the flagged queue with privacy protections. Switch between
  them with the "Demo as" buttons — that's expected, not a limitation.
- **Everything is local and disposable.** No data leaves your machine.

---

## 5. Stop / reset

```zsh
scripts/dev/start-stack.sh --down      # stop and remove all containers
```

Later, to start again:
```zsh
scripts/dev/start-stack.sh --no-build  # if images are already built
```

---

## 6. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| A build fails with **`input/output error`** or **`unexpected EOF`** | Google Drive hadn't finished syncing a file. It's transient — **just run `scripts/dev/start-stack.sh` again** (the script also retries automatically). |
| A build is **`Killed` / `container exited on killed`** | The VM ran out of memory. Give it more and retry: `podman machine stop && podman machine set --memory 12288 && podman machine start`, then `scripts/dev/start-stack.sh`. |
| The **"Demo as" buttons don't appear** | The portal is still starting, or dev mode isn't on. Wait ~20s and reload; confirm `http://localhost:8090/auth/dev/enabled` returns `{"enabled":true}`. |
| Submitting a claim says **"session expired / sign in again"** | Click a **"Demo as"** button again to refresh the session. |
| The page won't load at `http://localhost:8090` | Give the stack another ~30s after the script finishes, then reload. Check containers are up: `podman ps`. |
| Claims never get a score | The message pipeline needs a moment after start-up. Re-run `scripts/dev/start-stack.sh --no-build` (it re-checks the wiring), wait ~20s, and submit again. |

If you get stuck, `scripts/dev/start-stack.sh --down` then
`scripts/dev/start-stack.sh` gives you a clean slate.

---

## 7. What's running (for the curious)

`http://localhost:8090` is a single entry point (a reverse proxy) in front of:
the Claimant Portal (React), the Portal Backend, the Claim Intake + ROE
adapters, six detection engines (Repetitive Pattern, History Inconsistency,
Regional Analysis, Geographic Anomaly, Fraud Ring over a real graph database,
Document Forgery), an LLM service, an ensemble scorer, PostgreSQL, Kafka, Redis,
and Neo4j. The only ports published to your machine are **8090** (the app) and
**5432** (PostgreSQL, for inspection).

> **This is a local demonstration configuration.** The one-click role switcher
> and the auto-generated Record of Employment are development conveniences that
> stand in for the real GCKey sign-in and the employer's ROE Web submission.
> They are automatically disabled outside development.
