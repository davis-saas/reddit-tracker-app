# Reddit Tracker

Reddit Tracker keeps each client's **Reddit Pro numbers up to date in Google Sheets**.

You drop the three files Reddit Pro gives you (Posts, Comments, Account). Reddit Tracker saves dated
copies of the numbers on your computer, removes duplicates, and fills each client's Google Sheet with the
newest views, upvote rate, replies, shares and awards, plus each post's views after 7, 14 and 30 days.
It's free, and it runs on a Mac, a Windows PC, Linux or a Raspberry Pi.

**Contents:** [Privacy](#privacy) · [Install and start](#install-and-start) ·
[First use](#first-use) · [What's in the Sheet](#whats-in-the-sheet) · [How often to upload](#how-often-to-upload) ·
[Updating](#updating-to-a-new-version) · [Troubleshooting](#troubleshooting) ·
[For the person who publishes this app](#for-the-person-who-publishes-this-app) · [For developers](#for-developers)

---

## Privacy

- **Everything stays on your computer:** your client list, your Google sign-in and every number.
- **Your numbers go only to your own Google Sheets.** Nothing is sent anywhere else.
- **The people who made this app see nothing.** There's no account, no tracking and no server of ours.
- Google only lets Reddit Tracker open **the Sheets it makes and the ones you choose**. It can't see
  anything else in your Google Drive.
- To remove its access completely, go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

---

## Install and start

Download the zip file and unzip it. You get a folder called `reddit-tracker-app`. Keep the whole folder
together; you can put it anywhere (your Documents folder is fine).

Reddit Tracker opens in your web browser. **Keep its small window open while you use it.** Close the window
to stop it.

### Mac

1. Double-click the zip to unzip it.
2. Open the `reddit-tracker-app` folder and double-click **start-mac.command**.
3. The first time, macOS may say it can't check the file ("Apple could not verify…"). Click **Done**, then:
   - open **System Settings → Privacy & Security**,
   - scroll down to *"start-mac.command" was blocked*,
   - click **Open Anyway**, type your Mac password, then click **Open**.

   You only do this once.
4. If a box asks to install developer tools (for Python 3), click **Install**, wait until it's done, then
   double-click **start-mac.command** again. If it says this Mac's Python is too old, install the newest
   Python from [python.org/downloads](https://www.python.org/downloads/), then double-click it again.

Your browser opens Reddit Tracker. If it doesn't, open <http://127.0.0.1:8765>.

### Windows

1. Right-click the zip and choose **Extract All…**, then **Extract**. (Don't run it from inside the zip.)
2. Windows opens the new folder. Open the **reddit-tracker-app** folder inside it, then double-click
   **start-windows.bat**.
3. If Windows says *"Windows protected your PC"*, click **More info**, then **Run anyway**.
4. If it says Reddit Tracker needs Python 3.9 or newer:
   - go to [python.org/downloads](https://www.python.org/downloads/) and download Python,
   - run the installer and **tick "Add python.exe to PATH"** at the bottom of the first screen,
   - click **Install Now**, then double-click **start-windows.bat** again.

### Linux

Open a terminal in the `reddit-tracker-app` folder and run:

```
bash start-linux.sh
```

(You need Python 3.9 or newer. Most Linux systems already have it. If it says your Python is too old,
upgrade to a newer version of your Linux.)

### Raspberry Pi (runs all the time, use it from your laptop or phone)

You need Raspberry Pi OS Bullseye or newer (from 2021 on). On the Raspberry Pi, open a terminal (or connect
with SSH) and paste this one line:

```
cd ~ && curl -L -o reddit-tracker.zip DOWNLOAD-LINK && unzip -o reddit-tracker.zip && bash reddit-tracker-app/install-raspberry-pi.sh
```

(If it says `unzip: command not found`, run `sudo apt install unzip` and paste the line again.)

- Type your Raspberry Pi password if asked. Nothing shows while you type; that's normal.
- At the end it prints a link like `http://raspberrypi.local:8765/?key=…`.
- **Open that link on your laptop or phone and bookmark it.** Any device on the same network can use it.
  Keep the link private: it's the key.
- Reddit Tracker now starts by itself whenever the Raspberry Pi starts. Don't move or delete the
  `reddit-tracker-app` folder.
- If it says **Reddit Tracker is already open on this Raspberry Pi**, close the Reddit Tracker window on the
  Raspberry Pi's screen, then paste the line again.
- If it says **the system is too old**, put the newest Raspberry Pi OS on the memory card with
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

### Other options

`python3 reddit_tracker.py` (on Windows: `py reddit_tracker.py`) also takes:

| Option | What it does |
|---|---|
| `--port 8766` | Use another port (8765 is the usual one). |
| `--network` | Let other devices on your network use it, with the link it prints. |
| `--no-browser` | Don't open the browser. |
| `--data FOLDER` | Keep settings and numbers in another folder. |
| `--show-link` | Print the link for other devices, then stop. |

---

## First use

The **Getting started** box on the page walks you through it.

1. **Sign in with Google.** Click **Allow** on Google's page. Reddit Tracker only asks to use the Sheets it
   makes or you choose.
2. **Add a client.** Type their Reddit username (for example `Acme_Jane`) or paste their profile link
   (`reddit.com/user/Acme_Jane`).
3. **Choose their Google Sheet.** Click **Make a new Sheet** (easiest), or **Choose Sheet** to pick one
   you already have. You can also make one Sheet with **all** clients (Settings → *One Sheet with all
   clients*), handy for Looker Studio.
4. **Download the three reports from Reddit Pro.** Signed in to Reddit as the client, open
   **Reddit Pro → Performance**:
   - **Posts** tab: set the sort to **New**, then **Download CSV**.
   - **Comments** tab: set the sort to **New**, then **Download CSV**.
   - **Account** tab: choose **Past year**, then **Download CSV**.

   Don't open the files in Excel or Numbers first. Saving them there changes the dates and numbers.
5. **Drop the files on the page** (or click **Choose files**). Drop as many clients' files as you like at
   once. Reddit Tracker works out whose file is whose and asks you only when it can't tell.
6. Click **Import & update Sheets**. Your Sheets update a few seconds later.

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

**A ready-made Data Studio report for each client**

Open a client and click **Connect a Data Studio report**. Data Studio (Google's free report tool, once
called Looker Studio) opens a copy of the Undecided Agency report, already filled with that client's Sheet.
Click **Edit and share** to save the copy to your own Google account. It's yours: Reddit Tracker and the
template's owner can't see it. The first time, Data Studio may ask to see your Google Sheets: say yes.

To use your own report instead, build it on a Sheet made by Reddit Tracker (its charts read the **content**
tab), share it as *Anyone with the link can view*, then paste its link in Settings → **Data Studio
template**. You can also turn the button off there.

**Looker Studio tips**

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

On a Raspberry Pi, paste the same one line from [Raspberry Pi](#raspberry-pi-runs-all-the-time-use-it-from-your-laptop-or-phone)
again. It replaces the app and restarts it; your data stays.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| "Reddit Tracker needs Python" | Install Python 3.9 or newer (see [Install and start](#install-and-start)), then start it again. |
| "…Python is too old" or "the system is too old" | Mac or Windows: install the newest Python from python.org. Linux: upgrade to a newer version of your Linux. Raspberry Pi: put the newest Raspberry Pi OS on the memory card with Raspberry Pi Imager. |
| The page doesn't open | Open <http://127.0.0.1:8765> yourself. If another program uses that port, Reddit Tracker picks the next one and shows the address in its window. |
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

### Making a release

```
bash make-release.sh
```

It builds `dist/reddit-tracker-<version>.zip` with the app folder **and** `google_app.json` inside, and
checks that the start files can still run. It never uploads anything: publish the zip yourself (for example
as a GitHub release).

Before the first release, replace the two placeholders with your real addresses:

- `DOWNLOAD-LINK` in this README (the Raspberry Pi line) → the zip's download address.
- `README_URL` at the top of the script in `rt/page.html` → this README's address.

**Never commit `google_app.json` to git.** It's in `.gitignore`, and `make-release.sh` refuses to build if
git tracks it. (Google treats a Desktop app's secret as not truly secret, but keeping it out of public code
stops others from using your project.)

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
