# Setting up the Signal frontend

The Signal bot (`signal_bot.py`) reuses the same kimchi bridge as the
Telegram bot, so everything in the README (per-chat workspaces, persistent
sessions, security model, `KIMCHI_*` options) applies unchanged. The
difference is the messenger side: Signal has no bot tokens, so we run
[signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) in
Docker and link it to your Signal account as a companion device.
[signalbot](https://github.com/signalbot-org/signalbot) (the Python library)
does the rest.

## How it behaves

- You link **your** Signal account — the bot acts as an additional device on it.
- Only senders listed in `ALLOWED_SIGNAL_NUMBERS` trigger agent runs
  (private chats only; groups are ignored). Messages from anyone else
  arrive on your phone like normal Signal messages — the bot ignores them.
- While the agent works, the chat shows a typing indicator.
- Commands are plain text: `new`, `status`, `files`, `cancel`, `help`.

## One-time setup

1. Install [Docker](https://www.docker.com/).

2. Run signal-cli-rest-api in `normal` mode (needed once, for linking):

   ```bash
   docker run -p 8080:8080 \
     -v $(pwd)/signal-cli-config:/home/.local/share/signal-cli \
     -e 'MODE=normal' bbernhard/signal-cli-rest-api:latest
   ```

   (On Windows PowerShell, replace `$(pwd)` with an absolute path.)

3. Open <http://127.0.0.1:8080/v1/qrcodelink?device_name=kimchi-bot> and scan
   the QR code with your Signal app (Settings → Linked devices).

4. Stop that container, then start it again in `json-rpc` mode — this is the
   mode the bot uses day to day:

   ```bash
   docker run -d --restart unless-stopped -p 8080:8080 \
     -v $(pwd)/signal-cli-config:/home/.local/share/signal-cli \
     -e 'MODE=json-rpc' bbernhard/signal-cli-rest-api:latest
   ```

5. Verify it's up: open <http://127.0.0.1:8080/v1/about>.

6. Configure the bot — add to `.env`:

   ```
   SIGNAL_PHONE_NUMBER=+15551234567        # the Signal number you linked
   ALLOWED_SIGNAL_NUMBERS=+15551234567     # senders who may use the bot
   ```

7. Run it:
   - **Windows:** `run-signal.bat`
   - **macOS/Linux:** `./run.sh signal_bot.py`

8. Test: from an allowlisted phone, message your linked number a task like
   *"Create a file called notes.md with a packing list for a weekend trip."*
   Expect a reply within ~10–60 seconds (a real agent turn is running).

## Keeping it running

The Docker container **is** your Signal connection — the Python bot talks to
it on `localhost:8080`. Keep the container running (the `json-rpc` command
above uses `--restart unless-stopped`). The Python side can be auto-started
at logon like the Telegram bot (Scheduled Task on Windows, launchd/systemd
elsewhere) — point it at `signal_bot.py`.

## Troubleshooting

- **Bot doesn't respond:** check the container is running and `/v1/about`
  loads; check `signal-bot.log`; make sure sender numbers are E.164
  (`+countrycode...`) and match exactly.
- **`Configuration error: SIGNAL_PHONE_NUMBER...`** — the `.env` values are
  missing.

## Status & caveats

- Implemented against the signalbot 2.1.3 documented API and the
  signal-cli-rest-api setup guide. The Telegram frontend is fully
  end-to-end tested; the Signal frontend shares the same verified bridge but
  has not yet been exercised against a live linked account — if something is
  off, please open an issue.
- Don't unlink the device from your Signal app while the bot is in use.
