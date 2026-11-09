# Off-Cycle Radar

A small, self-hosted web app that finds **spring, fall, winter, off-cycle and co-op internships** for **software engineering and quantitative trading/research/development**, and sends application links when new matches appear.

![Status](https://img.shields.io/badge/runner-GitHub%20Actions-62edbc)

**What it includes**

- A responsive dark-mode dashboard with search, role/term filters, source health, sorting, and browser-local saved jobs.
- A Python scanner for **Greenhouse, Lever, Ashby**, and two public GitHub internship lists (a 2027 US tech list and the QuantScraper student list). Preconfigured company sources include Belvedere Trading, QuantCo, IMC, Point72, and others.
- Rule-based **summer exclusion**, **Spring/Fall/Winter/off-cycle/co-op matching**, undergrad-friendly filtering, and a separate *term unspecified* category. A label is based on posting text, **not a guarantee of eligibility or availability**.
- Deduplication, first-seen timestamps, cache reuse for a temporarily unavailable source (up to 72h), and alerts only for newly discovered matches.
- Notifications via **Telegram (recommended)**, **Discord webhook**, and/or **email SMTP**. Each alert includes an application link.
- GitHub Actions scanning twice daily, with persistent state committed to the repository; GitHub Pages can serve the dashboard without a server.

## 1. Run it locally (macOS / Linux / Windows)

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python tracker.py scan
python -m http.server 8000 --directory docs
```

Open **http://localhost:8000** in your browser. (Keep the local server running while viewing.)

**First scan behavior:** the dashboard fills with the current matches, but existing listings aren't notified by default, to avoid alert flooding. Later scans alert on eligible matches that have not already triggered an alert. Unsent matches remain queued until a notification channel is configured. To alert on existing listings during the first scan, run `python tracker.py scan --notify-existing` (before any normal first scan); alerts are capped at 25 links by default. To check what a scan would discover *without changing anything*, run `python tracker.py scan --dry-run`.

## 2. Telegram alerts (recommended)

1. Open Telegram, message **@BotFather**, use `/newbot`, and copy the bot token.
2. Start a conversation with your bot and send it a message.
3. Get your chat ID: open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in your browser and locate `message.chat.id`. (Replace the placeholder; do not share the token.)
4. Export the two environment variables locally, or store them as GitHub Actions **repository secrets** for unattended use:

```bash
export TELEGRAM_BOT_TOKEN='your-private-bot-token'
export TELEGRAM_CHAT_ID='your-chat-id'
python tracker.py test-notification
```

Never commit a token to Git, to `config.json`, or to the `docs/` folder.

### Alternatives

**Discord:** add a `DISCORD_WEBHOOK_URL` environment variable or GitHub Actions secret for your channel's webhook.

**Email:** set `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASSWORD`, and `EMAIL_TO` as environment variables or repository secrets. For Gmail SMTP, use `smtp.gmail.com`, port `587`, your Gmail address, and a Google **app password** where available; don't use your regular Google account password. Email delivery depends on the SMTP provider's configuration. Multiple channels can be enabled together.

After setting a channel up, test with `python tracker.py test-notification`. Test alerts use `example.com` and are **not real jobs**.

## 3. Run automatically with GitHub Actions

1. Create a **new GitHub repository**, and push all the files from this directory (including `.github/workflows/scan.yml`). If you publish the dashboard on GitHub Pages, the job listings will be public even if you keep personal notes elsewhere; use a dedicated repo.
2. In **Settings → Secrets and variables → Actions**, create **repository secrets** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (or the Discord/SMTP alternative above).
3. In **Settings → Actions → General → Workflow permissions**, enable **Read and write permissions** so scheduled scans can commit `docs/jobs.json` and `data/state.json`. If the default branch is protected against GitHub Actions pushes, adjust the rules or use an unprotected branch.
4. Go to **Actions → Scan off-cycle internships → Run workflow** to execute an initial scan. Optionally tick **notify_existing** on the first run to receive links for currently found matches.
5. To publish the web interface, open **Settings → Pages → Build and deployment → Deploy from a branch**, then select your default branch and **`/docs`**. GitHub will show the Pages URL in that Settings screen.

Scheduled scans run at **07:17 UTC and 19:17 UTC** (roughly 1:17 AM/PM CST or 2:17 AM/PM CDT). GitHub schedules can be delayed or occasionally skipped; this is a twice-daily best-effort monitor, not instantaneous alerts. GitHub may also disable scheduled workflows after prolonged inactivity in public repositories.

**Important:** the tracker does not run in the cloud just because you downloaded this ZIP. The GitHub steps above are required to turn on automatic checks and alerts. No credentials are included in this project.

## 4. Customize the opportunities

Edit `config.json`:

```json
{
  "target_years": [2027, 2028],
  "include_possible_offcycle": true,
  "notify_possible_offcycle": false,
  "skip_phd_only": true,
  "max_notifications_per_run": 25,
  "source_cache_hours": 72,
  "sources": [
    {"type": "greenhouse", "name": "Point72", "board": "point72"},
    {"type": "lever", "name": "ExampleCo", "board": "exampleco"},
    {"type": "ashby", "name": "ExampleCo2", "board": "exampleco2"}
  ]
}
```

That shortened example is illustrative; **keep the source list shipped in `config.json`** unless deliberately replacing it. For each ATS, `board` is the employer's public board token, **not necessarily their company name**. To add employers, copy their token from a verified careers URL such as `job-boards.greenhouse.io/<board>`, `jobs.lever.co/<board>`, or `jobs.ashbyhq.com/<board>`. Greenhouse European boards use `"region": "eu"`; Lever also accepts `"region": "eu"`. Invalid or offline boards are shown as failures in the dashboard and do not stop other sources.

**Filters:** a confirmed role must mention spring, fall/autumn, winter, off-cycle, non-summer, placement, or co-op in the title or description. A generic "Software Engineer Intern" appears as *Term unconfirmed* instead and **will not trigger an alert** unless `notify_possible_offcycle` is set to `true`. Explicit summer titles, PhD-only internships, and unrelated roles are excluded. There is intentionally **no global location restriction**; use the dashboard's location filter to narrow. The "United States" browser filter is heuristic and may miss locations.

## 5. Troubleshooting

- **Dashboard is empty:** run `python tracker.py scan` and refresh. Check source-health status. Some sources may be temporarily unavailable; curators also remove or close listings.
- **No notifications:** ensure the bot has received your first message, check your chat ID, and run `python tracker.py test-notification`. Existing postings won't alert after you've already run your baseline scan; new matches will.
- **GitHub Action cannot push:** check Actions workflow permissions, branch protection, and that your Actions runner can reach its data sources.
- **No data in GitHub Pages:** run the workflow, verify that `docs/jobs.json` changed, and allow Pages to deploy the change.
- **Not all companies are covered:** this is not a whole-internet crawler. It uses specific employer boards and third-party curated lists; some jobs never appear in these sources, and links can become stale.
- **Duplicate alerts after a failed notification:** if one channel accepts an alert and another fails, the next scan may retry the notification. State is advanced only when all configured deliveries succeed.

## Commands

```bash
python tracker.py scan
python tracker.py scan --dry-run
python tracker.py scan --notify-existing
python tracker.py test-notification
python -m unittest discover -s tests -v
```

## Privacy and safety

The scanner only requests publicly visible postings and sends links; it never logs in to employers, submits applications, bypasses access controls, or performs automated applications. Avoid adding sensitive personal data to this repo. Dashboard bookmarks are stored only in your browser. Bot tokens, email passwords and webhook URLs belong in GitHub Secrets or local environment variables.

**Public feed documentation:** [Greenhouse Job Board API](https://docs.greenhouse.io/job-board), [Lever Postings API](https://github.com/lever/postings-api), [Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api).
