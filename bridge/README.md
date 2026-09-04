# The bridge

The bridge runs on a Raspberry Pi or other always-on Linux device and connects Telegram to the Sticky. It passes messages typed in Telegram to the Sticky and button taps on the Sticky back to Telegram.

## What you need

A Raspberry Pi, or any always-on Linux machine.

The Telegram app and an account.

A free MQTT broker account like HiveMQ Cloud's free tier.

## Setting it up

### 1. Make the bot

In Telegram, message `@BotFather` and send `/newbot`. It will ask for a name (Beanstalk) and a unique
bot username, then replies with a setup message that will contain a token to access the API that looks like `8123456789:AAHk3xamPLEt0kenDoNotUse`. Copy the whole token string,
including the digits before the colon.

The same message has a `t.me/<username>` link. Open it and press Start. That
is the chat you will pair from.

### 2. Make two broker credentials on your MQTT broker

In HiveMQ, launch a free HiveMQ MQTT broker. Select the broker and then go into Access Management. Create two new credentials with publish and subscribe permissions. One for the panel, one for the bridge. You won’t be able to see the password again so save them for the next step.

The broker's hostname and port are on the Overview tab under Connection
Details. The hostname looks like `abcdef123.s1.eu.hivemq.cloud`; the port is
8883. `setup.html` asks for both.

Client ids are not something HiveMQ gives you. Each end names itself, and the
two must differ. Pick any two unique strings, like `bridge` and `sticky`. The
panel's is already set in the firmware; `setup.html` asks for the bridge's.

### 3. Fill in the setup page

Open `setup.html` from the repo root in a browser. It writes `secrets.yaml`
and `beanstalk.env`. Leave the chat id blank.

Put `secrets.yaml` in the repo root. Leave `beanstalk.env` in Downloads for the
next step.

### 4. Install on the Pi

`setup.html` prints these with your Pi's user and hostname filled in.

    scp ~/Downloads/beanstalk.env bridge/beanstalk.py bridge/beanstalk.service \
        bridge/install-beanstalk <pi>:~/
    ssh <pi> './install-beanstalk'

The Pi needs Raspberry Pi OS with SSH enabled. The script asks for your sudo
password. It sets up the service and starts it, and says if the service failed
to start, and why. If `python3-venv` is missing it says so and gives the apt
command.

### 5. Pair

`install-beanstalk` ends by printing a pairing code:

    Send this to your bot in Telegram:   MEADOW-4417

Send that to your bot. It replies `Paired.` That chat becomes the only one the
bridge answers.

If you missed the code:

    ssh <pi> 'sudo journalctl -u beanstalk | grep "PAIRING CODE"'

To pair a different chat later, delete `/var/lib/beanstalk/chat_id` on the Pi
and restart the service.

### 6. Turn on autocomplete

Type `/commands` to your Beanstalk bot in Telegram and copy that block.

Then send `/setcommands` to `@BotFather` and paste the block to enable autocomplete.


    buttons - set the three reply buttons
    jokes - show or replace the joke bundle
    joke - add one joke
    reboot - restart the panel
    status - link and battery
    help - how this works


## Sending a message

Type any message to your Beanstalk bot. Once received, it will display on the Sticky as a takeover screen.

Tapping the screen then shows the thread view and sends the (message read) receipt to Beanstalk bot.

Editing a message in Telegram sends the corrected text to the panel again.

Only the twenty emoji built into the firmware will render correctly. Others will show as an empty box.

## Reply buttons

    /buttons                     show the current set
    /buttons ans1, ans2, ans3    set your own
    /buttons silly               use a saved set
    /buttons default             back to the built-in three

Separate with commas or `|`. The set stays until you change it.

To set the buttons along with a message, end the message with a `?` line:

    Homework done? Dinner at 6.
    ?yes, no, not yet

Saved sets, editable at the top of `beanstalk.py`:

| Name | Buttons |
|---|---|
| `answer` | yes, no, not yet |
| `silly` | PFFFT 💨, poop, you smell |

Fewer than three leaves the extras blank. Labels over 32 characters are
refused.

## Emoji buttons

Fixed in the firmware.

| Button | Panel | You receive |
|---|---|---|
| 💨 | a fart noise | `PFFFFT! 💨` |
| 💩 | a joke on screen | `(read a joke)` |
| 😂 | a chirp | `HAHAHAHA! 😂` |
| ❤ | a chirp | `I love you ❤` |

## Jokes

Twenty ship in the firmware. To replace them, send the bot a `.txt` file, one
joke per line, `|` for a line break on screen:

    What did the poop say to the fart? | You blow me away.
    Why did the banana go to the doctor? | It was not peeling well.

    /jokes            what is loaded
    /joke <text>      add one; the oldest drops off after twenty
    /jokes default    back to the built-in twenty

Replacing the whole set is the `.txt` file only.

## Status

    /status

Reports the broker link, when the panel last checked in, and its battery.

You are also told, without asking, when the battery drops below 15%, when the
panel has been silent for twenty minutes, and when the bridge loses the broker
for five minutes or gets it back.

## Reboot

    /reboot

Restarts the panel. Messages, buttons, jokes and settings survive.

If the panel has lost wifi the command never arrives. The panel then raises
its own network and shows setup instructions on screen.

## Small things

Anything starting with `/` that is not a command is refused. To send a message
that starts with a slash, double it: `//like this`.

Only your chat is answered. Everything else is ignored and logged.

## Updating the bridge

Same script, run again:

    scp bridge/beanstalk.py <pi>:~/
    ssh <pi> './install-beanstalk'

It swaps in `beanstalk.py` and restarts the service. If the file is identical
to the installed one it says so. `/etc/beanstalk.env`, the venv and the paired
chat id are left alone unless you send a new `beanstalk.env`.
