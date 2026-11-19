# START HERE - ClipForge bot

You do not need to know how to code. Follow the steps in order. Every step says exactly what to click.
The steps are for **Windows**. On a Mac, use the Mac / Linux section of `README.md` (or ask me).

## What the bot does

Every 15 minutes it checks the YouTube channels in your list. When one of them posts a new video, the bot:

1. finds the most exciting ~55 seconds,
2. makes a vertical Short (1080x1920) with **captions**,
3. writes the title and description for you (credit and link to the original creator, plus "Subscribe to ClipForge"),
4. saves it, ready to post.

## Two honest limits (please read)

**1. Posting.** Google does not let a new, unapproved app post public videos. If a bot uploads a video, Google locks it as private and you cannot unlock it. So today the bot makes the finished Shorts and **you post them**. It takes about 1 minute per Short. Part C explains how to get fully automatic posting (it needs Google's approval, which I cannot give you).

**2. Cloud.** YouTube often blocks cloud servers from downloading videos. Your own computer is the reliable place for the bot. Part B lets you test the cloud for free in 5 minutes. If your computer is off for a while, the bot catches up on everything posted in the last 3 days when it is turned on again.

## Before you start: only add channels that said YES

The bot treats every channel in `channels.txt` as approved. Only add channels whose owners told you that you can clip their videos. Keep their message as proof. There is a message you can copy at the bottom of this page.
The file `candidates-top100-gaming.txt` lists the 100 biggest gaming channels (from vidIQ) as links you can **ask**. The bot does not read that file. When a creator says yes, copy their line into `channels.txt`.

---

## Part A - Run the bot on your Windows computer (about 20 minutes, one time)

**Step 1. Unzip.** Right-click the zip file, choose **Extract All**, then **Extract**. Move the new folder to `C:\ClipForge` (it is easier to find).

**Step 2. Set up.** Open the folder and double-click **SETUP-WINDOWS.bat**. A black window opens.
- If Windows asks "Do you want to allow this app to make changes?", click **Yes**.
- If it says "Part 1 is done. Now CLOSE this window", close it and double-click **SETUP-WINDOWS.bat** again.
- Wait until it says **DONE**. This can take 10-20 minutes. The window looks stuck sometimes; it is working.
- If you see red error text, take a photo of the window and show it to me.

**Step 3. Add your channels.** Right-click **channels.txt**, choose **Open with**, then **Notepad**. At the bottom, paste the link of each channel that said yes, one per line, for example:
```
https://www.youtube.com/@SomeGamer
https://www.youtube.com/@AnotherGamer
```
To get a link: open the channel on YouTube and copy the address from the top of the browser. Press **Ctrl+S** to save.

**Step 4. Make a test Short.** Double-click **TEST-ONE-VIDEO.bat**. Paste the link of one video (it must be longer than 2 minutes, and it must be yours or from a channel that said yes). Press Enter and wait a few minutes. When it says **Dry run OK**, open the folder `output\dry-run` and watch the `.mp4`. Is it vertical? Are there captions? Is the moment good?

**Step 5. That's it. The bot now runs by itself** every 15 minutes while your computer is on and you are signed in. The first check only remembers the videos that already exist. After that, every new upload is clipped. Finished Shorts appear in the folder `output\ready`. Double-click **OPEN-FINISHED-SHORTS.bat** to open it. Each Short has a `.txt` file with the same name, holding the title and description.

**Step 6. Post a Short (about 1 minute).**
1. Go to **studio.youtube.com** and make sure you are signed in as ClipForge.
2. Click **Create**, then **Upload videos**, then choose the `.mp4`.
3. Open the `.txt` file next to it. Copy the **title** and the **description** into YouTube.
4. Choose "No, it's not made for kids", click **Next** until **Visibility**, choose **Public**, click **Publish**.
YouTube shows vertical videos under 1 minute as Shorts automatically.

### Handy files in the folder
| File | What it does |
|---|---|
| `OPEN-FINISHED-SHORTS.bat` | opens the folder with your finished Shorts |
| `RUN-NOW.bat` | checks for new videos right now instead of waiting |
| `TEST-ONE-VIDEO.bat` | makes a test Short from one link |
| `SHOW-REPORT.bat` | shows what the bot has learned from your Shorts' views (Part D) |
| `UPDATE-YTDLP.bat` | updates the downloader (the bot also does it by itself every week) |
| `STOP-BOT.bat` | stops the bot running by itself |
| `pipeline.log` | a diary of what the bot did. Open it if something looks wrong |

If your computer is off, nothing runs. When it is on again, the bot checks all your channels and clips whatever was posted in the last 3 days.

---

## Part B - (Optional) Also run it for free in the cloud

Do this only if you want it to work while your computer is off. **Use the cloud OR your computer, not both** (they would make the same Shorts twice). Cloud steps are done in a web browser. Nothing to install.

**Step 1.** Go to **github.com** and create a free account.

**Step 2.** Click the **+** at the top right, then **New repository**. Name it `clipforge-bot`. Choose **Public** (public gets unlimited free run time; anyone can see your channel list and the finished clips). Click **Create repository**.

**Step 3. Upload the bot.** Click **uploading an existing file**. Open the unzipped bot folder on your computer, press **Ctrl+A** to select everything, and drag it onto the GitHub page. Wait for the upload bar, then click **Commit changes**.
- If the folder named `.github` did not upload, click **Add file > Create new file**. In the name box type `.github/workflows/shorts.yml`. Open `shorts-workflow.yml` from the bot folder in Notepad, copy everything, paste it in the big box, click **Commit changes**.

**Step 4. Add your channels.** In GitHub click **channels.txt**, click the pencil icon, paste your channel links under the last line, click **Commit changes**.

**Step 5. Test whether YouTube allows the cloud.** Click the **Actions** tab. If you see a green button that says "I understand my workflows, go ahead and enable them", click it. Click **ClipForge bot** on the left, click **Run workflow**, **tick the box** that says "Tick this to ONLY test...", and click the green **Run workflow** button. Wait about 3 minutes, refresh, and click the run.
- Green with a list of check marks: the cloud works. Switch off the bot on your computer (double-click `STOP-BOT.bat`). The bot will now run about every 15 minutes in the cloud.
- Red and it says YouTube is BLOCKING: the cloud does not work for you. Stay with Part A. You lose nothing.

**Where your Shorts appear in the cloud:** open your repository and click **Releases** on the right. Each new release holds the finished videos plus the title and description to copy. Tip: click **Watch**, **Custom**, tick **Releases** and GitHub emails you each time Shorts are ready.

Good to know: GitHub pauses scheduled runs after 60 days with no activity in the repository. If that happens, open the Actions tab and click **Enable workflow**. If you choose a Private repository, open `.github/workflows/shorts.yml`, change `*/15 * * * *` to `0 * * * *` (once an hour) or you may run out of free minutes.

---

## Part C - Posting by itself

Google does not let an app it has not approved post public videos: they are locked as private and you cannot unlock them. There are three ways around it.

1. **You post** (about 1 minute each). Works today. This is Part A.
2. **An approved app posts for you.** Apps that Google has already approved can publish public videos to your channel. In our chat I can see connectors for apps like OpusClip, vidIQ and Zapier. If you connect your YouTube channel to one of them, I can send finished Shorts through it while we are chatting. That is not 24/7, and free plans have limits.
3. **Google approves the bot itself.** Apply for free with the "YouTube API Services audit and quota extension form" (search for the name). It can take a while and Google may say no. Be honest in the form: it is a tool for your own channel and you use clips with the creators' permission. If they approve you, ask me and I will help you switch the bot to automatic posting. The code is already in the bot (`YT_DELIVERY=api`); it only needs your Google login.

---

## Part D - The bot learns what works

The bot watches how each of your Shorts does and changes what it does next time.

- **It measures.** It reads your channel's public video list (free, no login) and saves the views of each Short after 6, 24 and 72 hours. To match a posted Short to the clip it made, **post it with the title and description from the .txt file**. The description holds the link to the source video.
- **It tests, then follows the winners.** For every new Short it picks the look (blurred background or zoomed-in), the length (48, 53 or 58 seconds), the caption style and the title style. At first it tries everything. As results come in it uses the winners more and more, but keeps testing a little.
- **It picks better source videos.** Channels whose Shorts get views move to the front of the line. Source videos that are gaining views fast go first.
- **It learns from mistakes.** If a channel's videos keep failing, the bot pauses that channel for a week. If a channel's Shorts get far fewer views than your average, it pauses it for two weeks.
- **It tells you what it learned.** Double-click `SHOW-REPORT.bat` any time (it also saves `LEARNING-REPORT.md`). You see which options win, which channels work, the best hour to post, and a diary of what changed.

Be realistic: it needs about 30 measured Shorts before the patterns mean much, views are noisy, and YouTube decides how far a Short is shown, not this bot. The bot raises your odds; it cannot promise views. To switch learning off, set `YT_LEARNING=0`.

---

## A message you can copy to ask a creator for permission

> Hi [name], I really enjoy your videos. I run a small Shorts channel called ClipForge (https://www.youtube.com/@ClipForge-n3b). Would you be happy for me to post short vertical highlights (under 1 minute, with captions) from your public videos? Every Short would credit you by name and link to your full video, and I would take any clip down straight away if you ask. Thank you!

Save their reply. Channels that say yes are the ones to add to `channels.txt`. Small and medium channels often say yes because it brings them new viewers.

## If something goes wrong

- **Nothing shows up in `output\ready`.** Normal at first: the bot only clips videos posted after it started. Check `pipeline.log`.
- **The test says YouTube is blocking or asking you to sign in.** Try again later. If it keeps happening, tell me and I will help with the cookies option.
- **Downloads suddenly fail.** Double-click `UPDATE-YTDLP.bat`.
- **Anything with red text.** Take a photo of the window and show it to me.
