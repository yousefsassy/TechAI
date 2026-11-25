# TechStock AI

## React shopping assistant

Use the **EN / FR / عربي** switch in the header or on the sign-in screen. It changes the interface and the language of new assistant replies; Arabic uses a right-to-left layout. Shopping queries and cart commands accept English, French and Arabic, including Arabic-Indic numerals in budgets. Previous messages keep the language in which they were originally sent. The selected language is saved in the browser.

### Account and authenticator setup

For a personal Gmail address, run `python scripts/configure_gmail.py` from the project root. Enter your Gmail address and a Google **app password** when prompted; the password entry is hidden and the script updates the ignored `.env` without printing it or altering your OpenAI key. It configures one sender; users can register with other email addresses. Restart FastAPI afterward. Gmail requires Google 2-Step Verification before you can create an app password: [Google's app password instructions](https://support.google.com/accounts/answer/185833). The script configures `smtp.gmail.com` on port 587 with STARTTLS. The Gmail server/port are already prepared in `.env`; `EMAIL_MODE` stays in sandbox until you complete the interactive setup.

**Email codes are the default:** account creation and login send a six-digit code to your registered address. Codes expire after five minutes and work once; five failed attempts temporarily lock the account. Actual delivery requires your own SMTP account configured locally in `.env`:

```ini
EMAIL_MODE=real
SMTP_HOST=smtp.your-provider.example
SMTP_PORT=587
SMTP_USER=your-sender-address
SMTP_PASSWORD=your-provider-app-password
SMTP_FROM=your-sender-address
SMTP_ALLOWED_RECIPIENTS=your-login-address
```

Replace the sample values with your provider's real settings. Port 587 uses STARTTLS; port 465 uses implicit TLS. Restart FastAPI after editing `.env`. The existing `EMAIL_MODE=sandbox` and local SMTP settings capture messages for tests; they **do not send messages to your real inbox**. The backend returns a clear configuration error in that mode. Keep passwords and codes out of chat and Git. The OpenAI key does not send email.

The **Use an authenticator app instead** link keeps the earlier manual-key TOTP flow available. Existing authenticator accounts can also choose email codes once SMTP is configured. A single Gmail sender can email login codes to different user addresses. `SMTP_ALLOWED_RECIPIENTS` still restricts the original sandbox purchase-order emails, not shopping login codes. Both sign-in methods use the same shopping account.

The React shopping UI requires a local account with a password (at least 12 characters) and a time-based authenticator code. On first visit, select **Create an account**. Add the displayed setup key in Google Authenticator, Microsoft Authenticator, or another TOTP app using **Enter setup key / time based**, then enter its 6-digit code. Save the eight recovery codes shown once. Future sign-ins require your password and a new authenticator code; a recovery code can replace the authenticator code once if you lose your device. **Sign out** revokes the current session. Sessions expire after 24 hours and are stored only in browser tab session storage.

Password hashes use scrypt, authenticator secrets are encrypted with a generated key in `data/*.auth.key`, and recovery codes are stored as hashes. Preserve the encryption key file together with the SQLite database when moving or backing up the project; losing the key makes authenticator secrets unreadable. The original academic procurement endpoints still use their separate anonymous browser sessions and are not covered by this shopping login. This is a localhost demo; no password reset by email is provided.

The new React interface is in `frontend/`. It replaces the shopping-facing selection UI with conversation, saved catalog cards and a session-scoped cart; the original Streamlit procurement, approvals, orders and monitoring remain available for the academic workflow. From the repository root, start the existing API with `python scripts/run.py` (Python 3.11 and the pinned requirements installed). In another terminal:

```sh
cd frontend
npm install
npm run dev
```

Open **http://127.0.0.1:5173**. Set `LLM_PROVIDER=openrouter` and add your [OpenRouter API key](https://openrouter.ai/settings/keys) as `OPENROUTER_API_KEY` in the backend `.env`. The client requests `openrouter/free` and only sends that provider its own key. Restart the API after changing credentials. Free service availability and rate limits vary. With no model connection, literal shopping searches and basic comparisons still work. Open-ended answers require a model connection.

The assistant reads the conversation’s recent messages, active filters and last displayed cards. Common constrained searches, named comparisons and simple refinements run locally. Other requests use a single model decision to choose a general answer, card comparison or shopping search. Budget refinements keep the product in focus. A topic change starts a separate search within the same conversation.

Shopping searches first check SQLite. A recent match can answer immediately. A cache miss, an explicit live check, or missing retailer coverage for a price comparison can trigger parallel lookups at Spacenet and Scoop. It reads public category navigation and robots-advertised sitemaps, follows matching product links, verifies retailer metadata, and caches the observations. The collector limits pages and elapsed time per retailer. Repeated identical requests share a short SQLite cooldown. Product URLs are restricted to supported hosts. The crawler obeys robots rules and pauses on access or rate-limit responses. The app can return a partial result or an empty search when coverage is limited. No price or stock is inferred from sitemap URLs.

Cards group matching retailer offers before the result limit. Each comparison table includes prices in TND, differences from the lowest eligible offer, retailer links, stock and observation times. A card checked for this request is marked individually. General questions leave the last product set available for subsequent comparisons and cart commands. GPU comparisons use listed model tiers, which are estimates. The daily scheduled collection still runs alongside on-demand discovery. Use `SHOPPING_LIVE_DISCOVERY=false` for cache-only operation and `CATALOG_AUTO_REFRESH=false` to disable the separate scheduled job. The maximum age for recommendations is set by `CATALOG_MAX_AGE_HOURS` (48 by default). The authenticated `GET /shopping/catalog-status` endpoint reports the scheduled job’s status. Optional LangSmith traces cover tool selection, catalog search and retailer discovery.

See the [shopping architecture, Toprix investigation and performance measurements](docs/conversational-comparison.md).

With Docker, `docker compose up --build` exposes React on **http://127.0.0.1:5173**, the original Streamlit app on port 8501, and the API on port 8000. Run `python -m pytest tests/test_shopping.py -q` to verify conversation context, cart updates and session isolation.

Run `python -m eval.shopping_eval` after collecting real offers to audit category, brand and budget precision on three example queries; empty catalog scenarios report no score. This is a constraint check, not proof of model recommendation quality. Implementation inspiration: [Playwright's Python browser guide](https://playwright.dev/python/docs/pages) and a [Playwright e-commerce pagination walkthrough](https://www.youtube.com/watch?v=L9hXsdLe2V0); code is adapted to the existing Scrapling scraper and its source policies.

Intelligent IT procurement for Tunisia, built for CS496 Project 3 - The Operator.
Streamlit -> FastAPI -> LangGraph -> official MCP Python SDK -> SQLite.
Scrapling runs independently and collects retailer offers. Orders are internal sandbox records; no retailer checkout or payment exists.

## First-time setup (Windows)

Academic context: **CS496 Project 3 - The Operator**. Orders are simulated internal records, never real retailer purchases or payments. Local email mode captures messages; optional real SMTP sends actual email but still does not buy products.

Prerequisites: Git, **Python 3.11** on PATH, PowerShell, internet access to install pinned dependencies, and free localhost ports 8000, 8501 and 1025. No private database, retailer access, OpenAI key or Gmail account is needed for the first demo.

```powershell
git clone https://github.com/nourguidara/TechAI.git
cd TechAI
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -c "from backend.database import init_db; init_db()"
python -m sandbox.seed
python scripts/run.py
```

Only copy `.env.example` on first setup; preserve an existing `.env`. If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` instead of `python` in subsequent commands. The optional helper `powershell -ExecutionPolicy Bypass -File scripts/setup.ps1` creates the environment, installs dependencies and initializes tables; it preserves an existing `.env` and does not seed automatically. Run commands from the repository root.

Open **http://127.0.0.1:8501** for Streamlit, **http://127.0.0.1:8000/docs** for FastAPI Swagger, or **http://127.0.0.1:8000/health** for API health. Stop the launcher with Ctrl+C. Sandbox mode starts local SMTP and enforces 127.0.0.1:1025; `.eml` messages appear in `data/mail/`. Real mode never starts this capture service and preserves SMTP_HOST/PORT.

For the first purchase, open AI Assistant, expand **Structured request - no OpenAI credits required**, choose laptop, quantity 1, budget 3000 TND and a test address such as student@example.com. Find recommendations, select the DEMO product, review the proposal, then separately confirm. Check Purchase Orders and the captured email. No external email is sent with the default configuration. Demo offers expire under the same 48-hour freshness rule as other offers; use a fresh isolated `python scripts/smoke_demo.py` for a repeatable demo without altering your catalog.

Get your own funded OpenAI API key only for English/French natural-language extraction. Get your own LangSmith key only for optional tracing. Gmail is optional: use your own sender account/app password and explicit recipient allowlist as explained in `.env.example`; never share credentials in Git, issues, screenshots or chat. A teammate's keys and local `.env` are not supplied by this repository.

On Linux/macOS use `python3 -m venv .venv` and `source .venv/bin/activate`, then the equivalent Python commands above and `cp .env.example .env` for first setup.

## Repository map

```text
agent/                  LangGraph, extraction prompt, bounded actions
app/                    Streamlit home, assistant, catalog, orders, monitoring
backend/                FastAPI, schemas, models, trusted application services
mcp_server/             Six typed tools over genuine MCP stdio
scraping/               Scrapling collectors, parsing, source policies
sandbox/                Labeled synthetic fixtures, local SMTP, scoped resets
scripts/                Setup, launch, isolated demo, verification, report builder
tests/fixtures/         Minimal documented HTML parser fixture
tests/                  API, workflow, security, email and UI regression tests
eval/                   Reproducible scenario definitions and evaluation runners
                        Selected historical summaries retained as dated evidence
data/.gitkeep           Runtime directory; no private database needed
README.md               Onboarding and operating guide
verification.md         Measured checks and known integration blockers
report.md / report.pdf  Academic report; PDF is the canonical published copy
project3-operator.pdf   Academic assignment brief
threat_model.md          Security boundaries
errors.md / postmortem.md Development record
CONTRIBUTING.md          Team workflow and pre-PR checks
docs/repository-audit.md Publication scope and audit findings
.env.example            Placeholder configuration with sandbox defaults
requirements.txt        Pinned dependencies
Dockerfile / docker-compose.yml Optional container setup
```

Generated outputs and private trace links remain local. Evaluation definitions are tracked; current runner outputs are ignored. Historical summaries in `eval/` and the academic report are dated observations, not promises about a fresh catalog or live service availability.

## Catalog and demo

```powershell
python -m scraping.refresh_catalog --retailer spacenet --limit 34
```

The limit is **per category** (up to 102 offers across laptops, desktops and tablets). The scraper waits at least ten seconds between Spacenet requests. A complete run takes roughly 20 minutes, subject to network conditions. For a short run use `--category laptop --limit 3`. Re-running updates existing offers rather than duplicating them.

The UI catalog shows real records by default. Prices are observations with timestamps, not current retailer quotations. Unpublished stock quantities remain null. Freshness is limited to 48 hours for proposals and rechecked on order creation.

For a separately labeled offline demonstration:

```powershell
python -m sandbox.seed
```

This creates three explicitly marked DEMO offers; it does not claim they were scraped. Evaluation fixtures use a separate database. Do not run the seeder if you want a real-data-only development catalog.

In the AI Assistant, enter English or French requests, e.g. `Je veux 2 PC portables Lenovo, 16 Go RAM, SSD 512 Go, budget total 6000 TND.`
If OpenAI credits are unavailable, expand **Structured request - no OpenAI credits required**. This is explicit manual input, not a simulated AI response. It runs the same comparison, budget, interrupt, approval, order, and notification workflow.
Review the best recommendation and alternatives, select a product and quantity, then separately review the exact proposal, total, budget, and recipient. Confirm or reject. Selection does not approve a purchase. A confirmation dialog appears only after the database commit. Purchase history and monitoring are scoped to your browser session.

## Verified source policy

- **Spacenet:** public terms and robots inspected; normal HTTP access and structured metadata verified. Ten-second minimum crawl interval, no login or bypass. Category pagination and individual product pages are followed.
- **Scoop:** public robots file currently permits category/product pages and marks search indexing as allowed. The collector checks robots again before each run, obeys access blocks and follows pagination. Live extraction from the user's network still needs validation.
- **Mytek:** robots fetched; its category request returned HTTP 403. No bypass attempted. `--retailer mytek` can check normal access later and stops on blocks. Product parsing remains unverified against live Mytek product HTML.
- **Tunisianet:** public terms Article 9 prohibit reproduction of the catalog. Bulk collection is disabled. The command reports this limitation; obtain retailer permission before changing the policy.
- No fabricated products are inserted to meet a collection target. `data/scrape-*.json` contains run statistics, and `eval/catalog-verification.json` contains delivery-time database counts when available.

Sources: [Spacenet terms](https://spacenet.tn/content/3-terms-and-conditions-of-use), [Spacenet robots](https://spacenet.tn/robots.txt), [Mytek robots](https://www.mytek.tn/robots.txt), [Tunisianet terms](https://www.tunisianet.com.tn/content/3-conditions-generales-de-ventes).

## Architecture and workflow

`backend/` owns schemas, configuration, SQLAlchemy models and trusted transactions. `agent/graph.py` implements parse -> validate -> search -> compare -> authorized recommendations -> customer selection interrupt -> budget -> immutable proposal -> purchase approval interrupt -> create order -> notify -> finalize, with terminal branches for clarification, no feasible offer, rejection, and errors.

OpenAI performs structured requirement extraction. Search, ranking and recommendation explanations are deliberately deterministic. Quantity defaults to one; missing budget requires clarification. Unsupported hard constraints require clarification; soft preferences do not block the request; processor matching is conservative literal matching, not an unverified CPU-equivalence benchmark. One request currently purchases one configuration and quantity from one supplier.

`mcp_server/server.py` exposes six typed tools through **MCP SDK 2.3 MCPServer**, and `mcp_server/client.py` starts and connects to it over stdio. The server is a backend-owned subprocess, not an unauthenticated network service. Every graph tool invocation uses the MCP client. `get_offer_details` is also available for direct tool inspection.

| Tool | Purpose |
| --- | --- |
| search_products | Hard technical, budget, stock and freshness filtering |
| compare_offers | Revalidate candidates; rank by deterministic use-aware resource fit |
| check_budget | Decimal total and remaining merchandise budget |
| get_offer_details | Stored source, price and specification details |
| create_purchase_order | Approved, immutable proposal -> transactional sandbox order |
| send_confirmation_email | Durable outbox -> approved recipient only |

The graph uses durable SQLite checkpoints; an interrupt resumes with LangGraph Command. The resume payload never authorizes a purchase: the node and write service independently read the trusted proposal approval record. Authorization is tied to a hashed random session capability. Store access and MCP stdio are trusted local boundaries. This is a localhost university demo, not a production identity-management system.

## Recommendation policy and customer selection

English/French extraction distinguishes intended use, supported priorities, hard specifications, preferred brands and free-text soft preferences. A missing budget is never invented. Budget changes require a new request; selection accepts only the authorized offer ID and quantity (1-100).

`backend/services/ranking.py` implements versioned `resource-fit-v1`. After category, hard specification, stock, freshness and total-budget filtering, it normalizes relative weights across RAM, storage, SSD presence, processor information, completeness, budget headroom, resource-per-price value and preferred brand. RAM credit saturates at 32 GB and storage at 1024 GB; these are scoring caps, not inferred requirements. Processor presence earns information credit only. Unknown fields earn no positive credit. Headroom is 1 minus total/budget; value is the capped mean RAM/storage/SSD credit divided by total/budget. Each component and total estimate are available in typed API output, not displayed as a performance rating.

Programming emphasizes RAM; ML emphasizes RAM/storage; gaming and editing emphasize storage/resources but cannot establish performance without GPU/VRAM and benchmarks. Study/notes emphasize price and budget headroom. Multi-use weights are averaged; each supported explicit priority adds 20 relative weight units. Full base/profile weights are in the small policy module. Portability, battery and graphics priorities produce explicit unknown-data caveats; screen size does not imply portability. Free-text preferences are disclosed as unverified. This is a transparent heuristic, not an empirically validated buying model.

One best option and up to three alternatives show concrete price, memory, storage, brand, processor or supplier tradeoffs. Example **synthetic** programming fixtures with a 5000 TND budget: best Lenovo 32 GB/512 GB SSD at 2200 TND; Acer 16 GB/1 TB SSD at 2200 TND offers more storage and less RAM; a lower-priced Lenovo 16 GB/512 GB SSD at 1500 TND leaves more budget. No CPU-speed or gaming-FPS claim follows.

Both human checkpoints persist in SQLite. The server hashes an owner-bound recommendation snapshot, checks its 30-minute expiry and accepts only contained offer IDs. Quantity, current price, availability, freshness and hard requirements are checked again before proposal creation. Repeat selection returns the same proposal without approving it. The second checkpoint requires trusted approval of the exact immutable proposal hash. The normal path consumes 10 counted actions; retries share the same maximum of 12 across both checkpoints. Changing selection after proposal creation requires a new request. Existing sessions must retain their capability token to resume; no persistent login is provided.

## Database and integrity

- `products`: technical configuration, stable signature and original specifications.
- `supplier_offers`: supplier identity, source URL, price, availability, timestamp and demo marker.
- `shopping_selections`: immutable authorized recommendation snapshot/hash, owner, expiry and trusted product/quantity choice.
- `purchase_proposals`: immutable canonical payload/hash, owner, expiration and approval record.
- `purchase_orders` and `order_items`: unique proposal/idempotency keys and immutable price/product snapshots.
- `email_notifications`: one durable outbox record per order with delivery state and attempts.
- `agent_runs`: status, trajectory, counted steps, tokens, cost and timing.

Money uses Decimal and Numeric(14,3), matching TND millimes. SQLite foreign keys and WAL are enabled. BEGIN IMMEDIATE serializes approval/order writes; unique constraints prevent duplicate orders. The approved price must still equal the current offer price, and all requirements/budget are checked again before commit. Scrape refreshes cannot alter order snapshots.

Cross-retailer merging requires manufacturer identity and a complete matching configuration. When identity is uncertain, URLs keep records separate. This favors avoiding false merges over aggressive deduplication.

Budgets cover merchandise in the internal sandbox order. Shipping is not quoted or charged. General in-stock status does not establish an exact available quantity; the UI makes that limitation explicit.

## Configuration

See `.env.example`. Key options:

- Select OpenRouter with `LLM_PROVIDER=openrouter` and `OPENROUTER_API_KEY`. For an existing OpenAI integration, select `LLM_PROVIDER=openai` and configure `OPENAI_API_KEY` and `OPENAI_MODEL`.
- LANGSMITH_API_KEY / LANGSMITH_TRACING / LANGSMITH_PROJECT: optional tracing. Disabled tracing does not affect execution.
- DATABASE_URL: SQLite only. Development defaults to `data/techstock.db`; tests/evaluation use `test.db`/`evaluation.db`. Checkpoint files are derived from each database path.
- OFFER_MAX_AGE_HOURS: default 48.
- MAX_AGENT_STEPS: 1-12; MAX_TOOL_RETRIES: 0-2. Every tool attempt, including retries, consumes a step.
- EMAIL_MODE: sandbox by default. SMTP_HOST/PORT default localhost:1025. Real mode additionally requires SMTP_ALLOWED_RECIPIENTS, explicit credentials and TLS.
- INPUT_COST_PER_MILLION / OUTPUT_COST_PER_MILLION: optional verified model rates. Missing rates produce unknown cost, never a fabricated estimate.

Trace payloads are hidden by default to avoid exporting email addresses or raw content. Local trajectories retain validated tool arguments and retry/error timing. Monitoring can retrieve LangSmith trace links. The MCP subprocess also emits tool execution traces and timing logs.

## Email semantics

The order and outbox commit together. Failure to connect to SMTP records `failed` and permits a safe retry from order history. A crash or ambiguous SMTP acknowledgement leaves `sending` or `delivery_unknown`; those are deliberately not auto-retried. SMTP cannot guarantee exactly-once delivery across a crash after acceptance. Reconcile such messages in Mailpit/the SMTP sink before manually repairing their status. Message-ID is stable. Real recipient changes cannot be supplied through the email tool.

## Evaluation and tests

```powershell
python -m pytest -q
python -m eval.trajectory_eval
python -m eval.ranking_eval
python scripts/smoke_demo.py
```

The smoke script creates an isolated database with clearly marked synthetic fixtures and uses actual loopback SMTP on an ephemeral port. It selects an alternative with quantity two, approves separately, confirms twice and verifies one order and one email. It never connects to the running API or configured Gmail server.

62 workflow cases: the original 40 adapted for selection, plus 22 selection, use-case and safety scenarios. Nine separate ranking-policy cases use synthetic specifications; neither suite measures real-world recommendation accuracy. `eval/scenarios.json` defines requests, initial state, expected sequence/outcome, permitted writes, forbidden actions and stopping reasons. `eval/results.json` contains measured results. The offline runner uses real LangGraph, MCP transport and SQLite, a fixture parser, and an email transport stub. It measures control-flow behavior, **not live model quality**. Timing excludes MCP process startup and human deliberation. Model tokens/cost are zero only for this offline run.

Opt-in live model check:

```powershell
$env:RUN_LIVE_OPENAI='1'
python -m pytest tests/test_live.py -q
```

The delivery-time live check reached OpenAI but returned HTTP 429, no credits remaining. No successful live extraction or live token cost is claimed. LangSmith trace write/read and local SMTP delivery were verified separately. See `eval/live-status.json` and `verification.md`.

## Stopping proof

`consume` checks `steps < max_steps` before every counted action. A successful check increments steps exactly once; every retry calls the same function. Therefore by induction `0 <= steps <= max_steps <= 12`. At most two retries follow an initial tool attempt. Graph transitions are forward-only, except bounded retries. The human interrupt pauses with no polling loop; its replay does not increment a step before the interrupt. SDK network calls have timeouts; waiting for a human has no deadline but performs no autonomous work. Proposals expire after 30 minutes. Tests exercise bounds, rejection and forged resumes.

## Reset

Stop the application before resetting:

```powershell
python -m sandbox.reset --confirm
python -m sandbox.reset_mail --confirm
# Or preserve catalog while clearing orders, proposals, runs and checkpoints:
python -m sandbox.reset --confirm --orders-only
```

Database reset only accepts recognized database filenames directly under the project's `data` directory. It clears application tables, not unrelated databases. Local captured email reset only deletes `.eml` files under `data/mail`; Mailpit has its own UI. Tests initialize their own disposable database every case.

## Docker

```powershell
docker compose up --build
```

Includes backend (with MCP subprocess), Streamlit, and Mailpit. SQLite persists in the `catalog` volume. Mailpit UI: http://localhost:8025. All published ports bind localhost. The backend has a health check. Refresh in Docker: `docker compose exec backend python -m scraping.refresh_catalog --retailer spacenet --limit 34`.
Docker CLI is present in the delivery environment, but the engine was not running; image build and full Compose startup remain unverified.

## Known limits

Live OpenAI requires funded credits. Retailer access and selectors can change. Exact stock counts, shipping and missing technical details are not invented. Unknown/stale offers are excluded. CPU-equivalence and complex mixed-product baskets require further work. Browser-session identity is lost when Streamlit session state resets; persistent account login is outside this localhost demo. Offline evaluation cannot establish resistance of arbitrary future models or truthfulness of retailer prices. Browser visual automation was unavailable; page rendering and API behavior were tested with Streamlit AppTest and pytest.

## Contributions

Implementation, tests, data collection and documentation were generated with Codex in this workspace. Student names and actual team responsibilities were not provided and are not invented. Before submission, each team member should review the code and record their own contributions here.

## Troubleshooting

- **Port in use / Windows 10048:** stop the duplicate TechStock launcher or the process you own using the required port. Sandbox SMTP uses 1025; Gmail real mode does not start local SMTP. Do not kill unrelated services blindly.
- **Backend unavailable:** start from the project root using the virtual environment; check `/health` and terminal errors. Port 8000 serves the API, 8501 serves the UI.
- **No recommendations:** seed the clearly labeled demo offers on a new database, check category/budget/stock and the 48-hour freshness rule. Unknown hardware is not assumed to satisfy a hard requirement.
- **OpenAI 429 / missing key:** use structured mode or fund/configure your own key. Offline tests do not establish live language accuracy.
- **Real SMTP rejected:** verify your own app password and exact recipient allowlist locally. Never paste the password into logs or issues. Return to EMAIL_MODE=sandbox for local testing.
- **Missing imports:** run `python -m pip install -r requirements.txt` using the same interpreter that starts the application. `python -m pip check` checks installed dependency compatibility.
- **Expired selection or price change:** start a new request and review both checkpoints again. Selecting is not approving.

## Team collaboration

See [CONTRIBUTING.md](CONTRIBUTING.md). Work on feature branches, review pull requests and run offline checks before merging. Do not commit `.env`, SQLite files, emails, raw scraper captures, trace links or generated previews. Keep the academic report and documented fixtures. Publication audit and exact initial push commands are in [docs/repository-audit.md](docs/repository-audit.md).
