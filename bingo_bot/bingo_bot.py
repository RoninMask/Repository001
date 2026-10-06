#!/usr/bin/env python3
"""
Bingo Bot for macOS — plays a 5x5 phone bingo app through iPhone Mirroring.

What it does:
  * Reads your card once at the start of each game (Apple's built-in OCR).
  * Watches the ball area above the card for called numbers.
  * Clicks (daubs) each called number that is on your card.
  * Clicks BINGO every time a new row, column or diagonal completes,
    and keeps playing so you can collect multiple bingos.

Controls:
  * Ctrl+C          -> pause; then choose new game / recalibrate / quit
  * Slam the mouse into any screen corner -> emergency stop (pyautogui failsafe)

First run asks you to calibrate (hover the mouse + press Enter, 5 times).
The positions are saved to bingo_calibration.json next to this script.
"""

import json
import re
import sys
import time
from pathlib import Path

CALIBRATION_FILE = Path(__file__).with_name("bingo_calibration.json")

# --- Tunables ----------------------------------------------------------------
SCAN_INTERVAL = 0.3        # seconds between looks at the ball area
CONFIRM_SCANS = 2          # a number must be seen this many scans in a row
CLICK_PAUSE = 0.12         # pause after each daub click
BINGO_PAUSE = 0.6          # pause between BINGO clicks
CLICK_BINGO_PER_LINE = True  # True: one BINGO click per completed line
                             # False: one BINGO click per daub that completes
                             #        any number of lines at once
MAX_BALL = 75

COLS = "BINGO"
FREE = (2, 2)

# All 12 winning lines as lists of (row, col).
LINES = (
    [[(r, c) for c in range(5)] for r in range(5)] +          # rows
    [[(r, c) for r in range(5)] for c in range(5)] +          # columns
    [[(i, i) for i in range(5)], [(i, 4 - i) for i in range(5)]]  # diagonals
)


# --- Pure logic (no Mac libraries needed; easy to test) ----------------------

def parse_numbers(texts):
    """Pull bingo numbers (1..75) out of OCR text like 'B12', 'O-65', '7'."""
    found = set()
    for text in texts:
        # OCR sometimes reads the letter O as zero: 'O65' -> '065'.
        for tok in re.findall(r"\d+", text):
            if len(tok) == 3 and tok[0] == "0":
                tok = tok[1:]
            if len(tok) <= 2:
                n = int(tok)
                if 1 <= n <= MAX_BALL:
                    found.add(n)
    return found


def completed_lines(marked):
    """Indexes into LINES of every line whose 5 cells are all marked."""
    return {i for i, line in enumerate(LINES) if all(cell in marked for cell in line)}


def line_name(i):
    if i < 5:
        return f"row {i + 1}"
    if i < 10:
        return f"column {COLS[i - 5]}"
    return "diagonal \\" if i == 10 else "diagonal /"


class Game:
    """Tracks the card, what's daubed, and which bingos were already claimed."""

    def __init__(self, card):
        self.card = card                       # card[r][c] -> int, None at FREE
        self.where = {n: (r, c) for r, row in enumerate(card)
                      for c, n in enumerate(row) if n is not None}
        self.marked = {FREE}
        self.called = set()
        self.claimed = set()

    def daub(self, numbers):
        """Return cells to click for newly called numbers that are on the card."""
        to_click = []
        for n in sorted(numbers - self.called):
            self.called.add(n)
            cell = self.where.get(n)
            if cell and cell not in self.marked:
                self.marked.add(cell)
                to_click.append((n, cell))
        return to_click

    def new_bingos(self):
        new = completed_lines(self.marked) - self.claimed
        self.claimed |= new
        return sorted(new)


class Confirmer:
    """Only accept a number after it shows up in N consecutive scans (stops OCR blips)."""

    def __init__(self, needed=CONFIRM_SCANS):
        self.needed = needed
        self.streak = {}

    def update(self, seen):
        self.streak = {n: self.streak.get(n, 0) + 1 for n in seen}
        return {n for n, k in self.streak.items() if k >= self.needed}


# --- Mac side: screen, OCR, mouse --------------------------------------------

def mac_imports():
    global mss, pyautogui, Image, ocrmac
    try:
        import mss
        import pyautogui
        from PIL import Image
        from ocrmac import ocrmac
    except ImportError as e:
        sys.exit(f"Missing package ({e.name}). Run:\n"
                 "  python3 -m pip install mss pyautogui pillow ocrmac")
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0


_sct = None


def grab(left, top, width, height):
    """Screenshot a region given in screen points. Returns a PIL image."""
    global _sct
    if _sct is None:
        _sct = mss.mss()
    shot = _sct.grab({"left": int(left), "top": int(top),
                      "width": max(1, int(width)), "height": max(1, int(height))})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def ocr_texts(img, upscale=1):
    if upscale != 1:
        img = img.resize((img.width * upscale, img.height * upscale))
    results = ocrmac.OCR(img, recognition_level="accurate").recognize()
    return [r[0] for r in results]


def click(x, y):
    pyautogui.moveTo(x, y)
    pyautogui.click()


# --- Calibration ---------------------------------------------------------------

STEPS = [
    ("cell_tl", "the CENTER of the TOP-LEFT square of your card (B column, top row)"),
    ("cell_br", "the CENTER of the BOTTOM-RIGHT square of your card (O column, bottom row)"),
    ("balls_tl", "the TOP-LEFT corner of the area where called balls appear"),
    ("balls_br", "the BOTTOM-RIGHT corner of the area where called balls appear"),
    ("bingo", "the CENTER of the BINGO button"),
]


def calibrate():
    print("\nCalibration: for each item, hover the mouse over it and press Enter here.")
    print("(Keep the iPhone Mirroring window where it is afterwards — if you move it,")
    print(" recalibrate.)\n")
    cal = {}
    for key, desc in STEPS:
        input(f"  Hover over {desc}, then press Enter... ")
        x, y = pyautogui.position()
        cal[key] = [x, y]
        print(f"    saved ({x}, {y})")
    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2))
    print(f"\nSaved to {CALIBRATION_FILE.name}\n")
    return cal


def load_calibration():
    if CALIBRATION_FILE.exists():
        cal = json.loads(CALIBRATION_FILE.read_text())
        if all(k in cal for k, _ in STEPS):
            return cal
    return calibrate()


def cell_centers(cal):
    (x1, y1), (x2, y2) = cal["cell_tl"], cal["cell_br"]
    dx, dy = (x2 - x1) / 4, (y2 - y1) / 4
    return [[(x1 + c * dx, y1 + r * dy) for c in range(5)] for r in range(5)], dx, dy


# --- Reading the card ----------------------------------------------------------

def read_card(cal):
    centers, dx, dy = cell_centers(cal)
    card = [[None] * 5 for _ in range(5)]
    for r in range(5):
        for c in range(5):
            if (r, c) == FREE:
                continue
            x, y = centers[r][c]
            img = grab(x - dx * 0.45, y - dy * 0.45, dx * 0.9, dy * 0.9)
            nums = parse_numbers(ocr_texts(img, upscale=3))
            card[r][c] = nums.pop() if len(nums) == 1 else None
    return card


def print_card(card):
    print("\n     " + "    ".join(COLS))
    for r, row in enumerate(card):
        cells = []
        for c, n in enumerate(row):
            if (r, c) == FREE:
                cells.append("FR")
            else:
                cells.append(f"{n:2d}" if n else "??")
        print(f"  {r + 1}  " + "   ".join(cells))
    print()


def confirm_card(cal):
    """Read the card, then let the user fix anything OCR got wrong."""
    while True:
        card = read_card(cal)
        while True:
            print_card(card)
            missing = [(r, c) for r in range(5) for c in range(5)
                       if (r, c) != FREE and card[r][c] is None]
            if missing:
                print(f"  Couldn't read {len(missing)} square(s) (shown as ??).")
            ans = input("  Enter = looks right | R = rescan | fix e.g. G4=52 : ").strip().upper()
            if ans == "R":
                break
            m = re.fullmatch(r"([BINGO])\s*([1-5])\s*=\s*(\d{1,2})", ans)
            if m:
                c, r, n = COLS.index(m[1]), int(m[2]) - 1, int(m[3])
                if (r, c) != FREE:
                    card[r][c] = n
                continue
            if ans == "":
                if missing:
                    print("  Please fill in the ?? squares first.")
                    continue
                return card
            print("  Didn't understand that.")


# --- Main loop -----------------------------------------------------------------

def play(cal, card):
    centers, _, _ = cell_centers(cal)
    (bx1, by1), (bx2, by2) = cal["balls_tl"], cal["balls_br"]
    game, confirm = Game(card), Confirmer()

    # One click on the free space to make sure the mirroring window has focus.
    click(*centers[2][2])
    print("Playing! Ctrl+C to pause. Mouse into a screen corner = emergency stop.\n")

    while True:
        seen = parse_numbers(ocr_texts(grab(bx1, by1, bx2 - bx1, by2 - by1), upscale=2))
        called = confirm.update(seen)

        for n, (r, c) in game.daub(called):
            print(f"  {COLS[c]}{n} -> daub")
            click(*centers[r][c])
            time.sleep(CLICK_PAUSE)

        new = game.new_bingos()
        if new:
            print(f"  BINGO! {', '.join(line_name(i) for i in new)}")
            for _ in (new if CLICK_BINGO_PER_LINE else new[:1]):
                click(*cal["bingo"])
                time.sleep(BINGO_PAUSE)

        time.sleep(SCAN_INTERVAL)


def main():
    mac_imports()
    print("Bingo Bot — make sure the iPhone Mirroring window shows the bingo card.")
    cal = load_calibration()
    while True:
        card = confirm_card(cal)
        try:
            play(cal, card)
        except KeyboardInterrupt:
            pass
        except pyautogui.FailSafeException:
            print("\n  Emergency stop (mouse hit a corner).")
        ans = input("\nPaused. Enter = new game (rescan card) | C = recalibrate | Q = quit : ").strip().upper()
        if ans == "Q":
            break
        if ans == "C":
            cal = calibrate()


if __name__ == "__main__":
    main()
