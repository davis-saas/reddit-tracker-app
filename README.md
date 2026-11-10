# Reddit Tracker

**Drop your clients' Reddit Pro reports in. Their Google Sheets fill themselves.** Free. Works on Mac,
Windows and Raspberry Pi.

## 1. Download

### 👉 [Download Reddit Tracker](https://github.com/davis-saas/reddit-tracker-app/archive/refs/heads/main.zip)

Unzip it. You get a folder called `reddit-tracker-app-main`. Put it anywhere (Documents is fine).

## 2. Start it

**Mac:** open the folder, double-click **start-mac.command**.
- Mac says it can't check the file? Click **Done**, open **System Settings → Privacy & Security**, scroll
  down, click **Open Anyway**. Only the first time.
- Mac asks to install developer tools? Click **Install**, wait, then double-click the file again.

**Windows:** right-click the zip → **Extract All**. Open the folder, double-click **start-windows.bat**.
- "Windows protected your PC"? Click **More info → Run anyway**.
- It says you need Python? Get it from [python.org/downloads](https://www.python.org/downloads/), and on the
  first screen of the installer **tick "Add python.exe to PATH"**. Then double-click the file again.

**Raspberry Pi:** open a terminal and paste this one line:

```
cd ~ && curl -L -o reddit-tracker.zip https://github.com/davis-saas/reddit-tracker-app/archive/refs/heads/main.zip && unzip -oq reddit-tracker.zip && mkdir -p reddit-tracker-app && cp -R reddit-tracker-app-main/. reddit-tracker-app/ && rm -rf reddit-tracker-app-main reddit-tracker.zip && bash reddit-tracker-app/install-raspberry-pi.sh
```

It prints a link. Open it on your laptop or phone and bookmark it. ([More about the Raspberry Pi](#raspberry-pi-details))

Reddit Tracker opens in your browser. **Keep its small window open while you use it.** If the page doesn't
open, double-click the start file again.

## 3. Use it

1. Click **Sign in with Google**, then **Allow**.
2. Click **Add client** and type their Reddit username.
3. Click **Make a new Sheet**.
4. Download the client's 3 reports from Reddit Pro (below), drop them on the page, and click
   **Import & update Sheets**.

Done. Do step 4 again whenever you want fresh numbers.

## How to download the reports from Reddit Pro

Log in to Reddit as the client. In the left menu, under **REDDIT PRO**, click **Performance**. Then:

| Tab | Set this | Click |
|---|---|---|
| **Posts** | sort: **New** | **Download CSV** |
| **Comments** | sort: **New** | **Download CSV** |
| **Account** | **Past Year** | **Download CSV** |

Don't open the files in Excel or Numbers first. Just drop them on Reddit Tracker. You can drop files for
many clients at once: it works out whose file is whose and asks only when it can't tell.

**Tip:** Reddit's Comments file only has the newest 100 comments. For busy accounts, upload at least every
couple of weeks (the app tells you when).

**Bonus:** on a client's page, **Connect a Data Studio report** makes your own ready-made report for that
client ([more](#a-ready-made-data-studio-report-for-each-client)).

---

**Everything else:** [Privacy](#privacy) · [Raspberry Pi details](#raspberry-pi-details) ·
[What's in the Sheet](#whats-in-the-sheet) · [Updating](#updating-to-a-new-version) ·
[Troubleshooting](#troubleshooting) · [For the person who publishes this app](#for-the-person-who-publishes-this-app) ·
[For developers](#for-developers)

---

## Privacy

**What stays on your computer:** your client list, your Google sign-in, every number, and a copy of every report
you drop in (in the data folder, until you delete it). Only your computer account can open that folder, and only
your browser can open the app: it opens with a private key from the start file, so other accounts and programs
on the same computer are turned away.

**What goes to Google, and only to Google:** the numbers, post titles and client names that fill **your own**
Google Sheets. Reddit Tracker can only open the Sheets it makes and the ones you choose, never anything else in
your Google Drive.

**What the people who made this app can see:** no names, no emails, no clients, no numbers. There's no account,
no tracking and no server of ours. Two kinds of **totals** exist outside the app: Google shows us how many
requests and sign-ins this app's Google key handles in total, and GitHub shows how many people visit or
download this page. If you want no link to us at all, make your own Google key (about 15 minutes, see
[For the person who publishes this app](#for-the-person-who-publishes-this-app)) and replace `google_app.json`.

**Data Studio reports:** a copy made with **Connect a Data Studio report** belongs to you. It never keeps the
template's Google Analytics, so nobody is told when you open it.

**Phone or Raspberry Pi (network mode):** the link works on your home or office Wi-Fi and isn't encrypted, so
don't use it on public Wi-Fi, and keep the link private: it's the key.

**Signing out:** **Sign out** forgets the sign-in on this computer. **Sign out and remove access** also asks Google
to cancel Reddit Tracker's permission. You can always remove it yourself at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

---

## Raspberry Pi details

- You need Raspberry Pi OS Bullseye or newer (from 2021 on).
- If it says `unzip: command not found`, run `sudo apt install unzip` and paste the line again.
- Type your Raspberry Pi password if asked. Nothing shows while you type; that's normal.
- The link looks like `http://raspberrypi.local:8765/?key=…`. Any device on the same network can use it.
  Keep it private: it's the key.
- Reddit Tracker starts by itself whenever the Raspberry Pi starts. Don't move or delete the
  `reddit-tracker-app` folder.
- **Reddit Tracker is already open on this Raspberry Pi**: close the Reddit Tracker window on the Raspberry
  Pi's screen, then paste the line again.
- **The system is too old**: put the newest Raspberry Pi OS on the memory card with
  [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (this erases the card), then paste the line again.

Lost the link? On the Raspberry Pi, run:

```
python3 ~/reddit-tracker-app/reddit_tracker.py --show-link
```

You can also copy it from **Settings → Use from other devices** on a device that already works there.

**Signing in from a laptop or phone (the copy-and-paste step).** Google sends you back to an address on
"this device", which your phone or laptop can't open. So after you click **Allow**, your browser shows an
error page such as *"This site can't be reached"*. That's expected:

1. Click the address bar at the top of that error page.
2. Select all of the address and copy it.
3. Go back to the Reddit Tracker tab, paste it into the box, and click **Finish signing in**.

Choosing a Sheet from a laptop or phone uses the same step. Making a new Sheet doesn't need it.

To stop the Raspberry Pi service (your clients and numbers stay): `bash ~/reddit-tracker-app/install-raspberry-pi.sh --uninstall`

**Linux:** open a terminal in the folder and run `bash start-linux.sh` (needs Python 3.9 or newer).

**Other options.** `python3 reddit_tracker.py` (on Windows: `py reddit_tracker.py`) also takes:

| Option | What it does |
|---|---|
| `--port 8766` | Use another port (8765 is the usual one). |
| `--network` | Let other devices on your network use it, with the link it prints. |
| `--no-browser` | Don't open the browser. |
| `--data FOLDER` | Keep settings and numbers in another folder. |
| `--show-link` | Print the link for other devices, then stop. |

---

## What's in the Sheet

Each Sheet has three tabs. Reddit Tracker rewrites them after every import, so don't type in them. Add your
own tabs if you want: Reddit Tracker never changes tabs it didn't make.

**content** — one row per post or comment, with its newest numbers:

| Column | Meaning |
|---|---|
| `kind` | `post` or `comment` |
| `account` | the Reddit username |
| `posted_date`, `posted_month` | when it was posted |
| `subreddit`, `title` | where, and the post's title (for a comment: the post it's on) |
| `text` | always empty: Reddit's files don't include comment text |
| `item_type` | for example Text, Image, Link |
| `as_of` | the day of the newest numbers |
| `views`, `upvote_rate`, `replies`, `shares`, `awards` | the newest numbers |
| `views_7d`, `views_14d`, `views_30d` | views 7, 14 and 30 days after posting (blank until known) |
| `url`, `item_key` | a link (comments always; posts when Reddit Tracker can find it) and an ID |

**account_daily** — one row per account per day: `account`, `date`, `views`, `upvotes`, `followers`.

**content_history** — every saved snapshot: `export_date`, `item_key`, `kind`, `account`, `posted_date`,
`age_days`, `views`, `upvote_rate`, `replies`, `shares`, `awards`. Good for growth charts.

### A ready-made Data Studio report for each client

Open a client and click **Connect a Data Studio report**. Data Studio (Google's free report tool, once
called Looker Studio) opens a copy of the Undecided Agency report, already filled with that client's Sheet.
Click **Edit and share** to save the copy to your own Google account. It's yours: Reddit Tracker and the
template's owner can't see it. The first time, Data Studio may ask to see your Google Sheets: say yes.

To use your own report instead, build it on a Sheet made by Reddit Tracker (its charts read the **content**
tab), share it as *Anyone with the link can view*, then paste its link in Settings → **Data Studio
template**. You can also turn the button off there.

### Data Studio tips

- Add the Sheet as a Google Sheets data source and pick the tab you want.
- Set `posted_date` (and `date`, `as_of`, `export_date`) to the **Date** type.
- Set `upvote_rate` to **Average**, not Sum.
- Each client's **Download CSV** button (in the client's Edit box) gives you their content tab as a file.

---

## How often to upload

Whenever you like. There's no schedule.

One thing to know: **Reddit's Comments file only lists the newest 100 comments.** For a busy account, older
comments drop out of the file and stop updating. If Reddit Tracker shows *"Comments only reach back N
days"*, upload that client again within N days to keep every comment. Posts and Account days don't have
this problem.

---

## Updating to a new version

Your clients, Google sign-in and numbers live in your **data folder**, not in the app folder:

| Computer | Data folder |
|---|---|
| Mac | `~/Library/Application Support/Reddit Tracker` |
| Windows | `%APPDATA%\Reddit Tracker` |
| Linux, Raspberry Pi | `~/.local/share/reddit-tracker` |

So updating is easy: close Reddit Tracker, unzip the new version, and start it the same way. You can delete
the old app folder. (The page shows the data folder under Settings.)

On a Raspberry Pi, paste the same one line from [Start it](#2-start-it)
again. It replaces the app and restarts it; your data stays.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| "Reddit Tracker needs Python" | Install Python 3.9 or newer (see [Start it](#2-start-it)), then start it again. |
| "…Python is too old" or "the system is too old" | Mac or Windows: install the newest Python from python.org. Linux: upgrade to a newer version of your Linux. Raspberry Pi: put the newest Raspberry Pi OS on the memory card with Raspberry Pi Imager. |
| The page doesn't open, or says to use the start file | Double-click the start file again: it opens the page with this computer's key. Typing <http://127.0.0.1:8765> yourself only works in the browser that opened it before. If another program uses that port, Reddit Tracker picks the next one and shows the address in its window. |
| "Reddit Tracker isn't answering" | Its window was closed or the computer slept. Start it again; your dropped files stay on the page. |
| "Google isn't set up in this copy" | This copy has no `google_app.json`. Ask whoever gave you the app for a complete copy. |
| "Google no longer accepts this copy of Reddit Tracker" | Download the newest Reddit Tracker and start it the same way. Your clients and numbers are kept. |
| Google says "Access blocked" or "This app isn't verified" | The person who published the app has to finish their Google setup (see below). |
| "This computer couldn't check Google's security certificate" | Check the computer's date and time. On a Mac with Python from python.org, open Applications → Python 3.x → **Install Certificates.command**. |
| "Sign in with Google again" | Google ended the sign-in (for example after a password change). Click **Sign in again**. Your Sheets catch up by themselves. |
| A Sheet says "can't open this Sheet any more" | It was deleted, moved to the trash, or chosen in another Google account. Click **Choose Sheet again**. |
| "This Sheet has N rows … but Reddit Tracker has only M" | Reddit Tracker won't wipe a Sheet that holds more than it has. Press **Keep the Sheet's numbers** to copy them onto this computer (nothing it already has is replaced), or **Use this computer's numbers** to overwrite the Sheet. Or make a new Sheet. |
| "Some Sheets weren't updated" | Your numbers are saved. It tries again by itself every 5 minutes, or click **Try again**. |
| "This file was opened and saved in Excel or Numbers" | Download it again from Reddit Pro and drop it without opening it. |
| "This file looks sorted by Top" | In Reddit Pro set the sort to **New** and download it again. |
| "This device needs the Reddit Tracker link" | Open the link with `?key=` again. To see it, run `--show-link` on the computer where Reddit Tracker runs (for example the Raspberry Pi). |
| After allowing Google on a phone: "This site can't be reached" | That's expected: copy the whole address and paste it into Reddit Tracker (see the copy-and-paste step above). |
| "Reddit Tracker's settings file can't be read" | Click **Restore the last good copy**. |
| "Reddit Tracker is already open on this computer (or Raspberry Pi), but only for…" | Another Reddit Tracker window is open there. Close it, then start Reddit Tracker (or the Raspberry Pi installer) again. |
| Raspberry Pi: nothing answers | Run `systemctl status reddit-tracker`, or `journalctl -u reddit-tracker -n 50` to see why. |

Reddit Tracker also keeps a log in `log.txt` in the data folder. It never contains passwords, sign-in codes
or keys.

---

## For the person who publishes this app

People who download Reddit Tracker sign in to **their own** Google account, but Google needs to know which
app is asking. That's `google_app.json`, which you make once and ship inside the zip.

### Google Cloud setup (once, about 15 minutes)

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and create a project called
   **Reddit Tracker**.
2. **APIs & Services → Library**: enable **Google Sheets API**, **Google Drive API** and **Google Picker API**.
3. **Google Auth Platform → Branding** (the OAuth consent screen): click **Get started**.
   - App name: **Reddit Tracker**; user support email: yours.
   - Audience: **External**. Contact email: yours. Agree and create.
   - Don't upload a logo (a logo means Google has to review the app).
4. **Data access → Add or remove scopes**: add only `https://www.googleapis.com/auth/drive.file`, then save.
   Nothing else.
5. **Audience → Publish app**, so the publishing status is **In production**. drive.file is a
   non-sensitive permission, so no verification is needed. (Left in *Testing*, every sign-in stops working
   after 7 days and only listed test users can sign in.)
6. **Clients → Create client**: application type **Desktop app**, name it, click **Create**, and
   **download the JSON right away**. Save it as `google_app.json` next to `reddit_tracker.py`.
7. Try it: `python3 reddit_tracker.py` (Windows: `py reddit_tracker.py`), sign in, make a Sheet, choose a Sheet.

Keep that Google client. If you delete it or reset its secret, every copy you already gave out stops
working ("Google no longer accepts this copy of Reddit Tracker") until people download a new zip.

### The Google file is in this repo

`google_app.json` is kept in this repo, so the **Download** link above gives a copy that works straight
away. Google treats a Desktop app's key as not truly secret: it can't open anyone's data, and every person
still signs in and clicks **Allow** themselves. If the key is ever misused, make a new client in Google
Cloud (step 6 above), replace `google_app.json` here and push: new downloads use the new key. Keep the old
client until people have updated, because deleting it stops their copies signing in.

`bash make-release.sh` can still build a zip (`dist/reddit-tracker-<version>.zip`) if you'd rather hand out
a fixed version.

---

## For developers

- **Zero dependencies:** Python 3.9+ standard library only. No pip, no venv.
- **Run the tests:** `python3 tests/run_tests.py` (Windows: `py tests\run_tests.py`; add `-v` for names, or a
  name like `server` for one file).
  The tests use a strict fake Google (`tests/fake_google.py`) and synthetic data only; nothing touches the
  network.
- **Test mode:** set `RT_GOOGLE_BASE=http://127.0.0.1:<port>` to send every Google call to a local fake. The
  page shows a yellow test-mode bar. `RT_DATA_DIR` (or `--data`) picks the data folder;
  `RT_GOOGLE_APP_FILE` picks the Google app file.

```
reddit_tracker.py      start here: options, then rt.server.main()
rt/server.py           web server: page, API, /oauth, who may use it, /api/status
rt/page.html           the whole page (HTML, CSS and JavaScript in one file)
rt/engine.py           preview and import: whose file is it, is it safe, what does it add
rt/csvfiles.py         reading and checking Reddit Pro's CSV files
rt/store.py            the SQLite database and the tables for the Sheets
rt/settings.py         settings.json: clients, the All clients Sheet, options
rt/google.py           Google sign-in (PKCE, loopback or paste) and Sheets/Drive calls
rt/sheets.py           writing the tabs, and the background Sheets job
rt/paths.py            where files live
tests/                 run_tests.py, builders.py (synthetic CSVs), fake_google.py, test_*.py
start-*.{command,bat,sh}, install-raspberry-pi.sh, make-release.sh
```

Not yet checked against the real Google: whether the file chooser's answer always carries `state` (the app
accepts one without it only when exactly one choice is waiting), and the exact wording of the "cell limit"
error.

Licence: MIT (see `LICENSE`).
