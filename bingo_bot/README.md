# Bingo Bot (MacBook + iPhone)

Plays your 5x5 phone bingo app from the Mac. It reads the called balls, clicks the
matching squares, and clicks **BINGO** every time a new line completes. It keeps going
after each bingo, so you can collect several in one game.

## 1. One-time setup

**iPhone Mirroring** (shows your iPhone in a window on the Mac):
- Needs macOS Sequoia (15) or later on the Mac and iOS 18 or later on the iPhone, signed into the same Apple ID.
- Open the **iPhone Mirroring** app on the Mac (it's in Applications) and follow the prompts.
- Open your bingo app inside that window.

> If your Mac has an M-series chip, many iPhone apps can also be installed straight from
> the Mac App Store (search for the app and check the "iPhone & iPad Apps" tab). That works too.

**Python and packages:** open **Terminal** and run:
```
xcode-select --install          # only if python3 isn't installed yet
python3 -m pip install mss pyautogui pillow ocrmac
```

**Permissions:** go to System Settings → Privacy & Security and turn on **Terminal** under both:
- **Screen Recording** (so the bot can see the game)
- **Accessibility** (so the bot can click)

Quit Terminal and reopen it after changing these.

## 2. Run it

```
cd path/to/bingo_bot
python3 bingo_bot.py
```

**First run: calibration.** Start a game so the card is showing. The bot asks you to hover
the mouse over 5 spots and press Enter for each one:
1. The center of the top-left square
2. The center of the bottom-right square
3. The top-left corner of the area where the balls show
4. The bottom-right corner of that ball area
5. The BINGO button

This is saved in `bingo_calibration.json`, so you only do it once. **Don't move or resize
the iPhone Mirroring window afterwards.** If you do, press **C** at the pause menu to
recalibrate.

**Each game:** the bot reads your card and prints it. Check it against the screen:
- **Enter** if it's right
- **R** to rescan
- To fix a square, type the column letter, the row number and the correct value, for example `G4=52`

Then it plays.

**Controls**
- **Ctrl+C** pauses. Press **Enter** for a new game, **C** to recalibrate or **Q** to quit.
- **Emergency stop:** move the mouse hard into any corner of the screen.

## Tweaks (top of `bingo_bot.py`)
| Setting | What it does |
|---|---|
| `CLICK_BINGO_PER_LINE` | `True` clicks BINGO once for each completed line. Set it to `False` if your app claims all lines with one tap, or penalizes extra taps. |
| `SCAN_INTERVAL` | How often the bot looks at the balls, in seconds. |
| `CONFIRM_SCANS` | How many scans in a row a number must appear in before it's trusted. Raise it if the bot daubs wrong numbers. |
| `CLICK_PAUSE`, `BINGO_PAUSE` | Pauses after clicks. Raise them if the app misses taps. |

## Troubleshooting
- **It doesn't click anything:** check the Accessibility permission for Terminal.
- **The screenshots are black, or it reads nothing:** check the Screen Recording permission.
- **It misses balls:** recalibrate with the ball box drawn a bit bigger around the ball area.
- **It reads old balls from the history strip:** draw the ball box around only the newest
  ball(s). Old numbers that are already daubed are ignored anyway.
