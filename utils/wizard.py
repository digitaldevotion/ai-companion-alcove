#!/usr/bin/env python3
"""Interactive menu wizard for Alcove utilities."""

import os
import signal
import subprocess
import sys
import textwrap

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class C:
    """ANSI color / style constants."""
    RST = "\033[0m"
    BLD = "\033[1m"
    DIM = "\033[2m"
    ITA = "\033[3m"
    UND = "\033[4m"
    BLK = "\033[30m"
    RED = "\033[31m"
    GRN = "\033[32m"
    YEL = "\033[33m"
    BLU = "\033[34m"
    MAG = "\033[35m"
    CYN = "\033[36m"
    WHT = "\033[37m"
    BRED = "\033[1;31m"
    BGRN = "\033[1;32m"
    BYEL = "\033[1;33m"
    BBLU = "\033[1;34m"
    BCYN = "\033[1;36m"
    BWHT = "\033[1;37m"
    BG_BLK = "\033[40m"
    BG_RED = "\033[41m"
    BG_GRN = "\033[42m"
    BG_BLU = "\033[44m"
    BG_CYN = "\033[46m"
    BG_WHT = "\033[47m"

    @staticmethod
    def supports_color():
        if os.getenv("NO_COLOR") is not None:
            return False
        if not hasattr(sys.stdout, "isatty"):
            return False
        if not sys.stdout.isatty():
            return False
        if os.name == "nt":
            os.system("")  # enable VT100 on Windows
            return True
        term = os.getenv("TERM", "")
        return term != "dumb"


_ENABLED = C.supports_color()


def s(code, text):
    return f"{code}{text}{C.RST}" if _ENABLED else text


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


W = 60

MENU_ITEMS = [
    {
        "key": "1",
        "label": "Generate / Configure config.py",
        "tag": "CONFIG",
        "tag_color": C.CYN,
        "color": C.WHT,
    },
    {
        "key": "2",
        "label": "Migrate / Upgrade Installation",
        "tag": "MIGRATE",
        "tag_color": C.YEL,
        "color": C.WHT,
    },
    {
        "key": "3",
        "label": "Install / Update Dependencies",
        "tag": "INSTALL",
        "tag_color": C.GRN,
        "color": C.WHT,
    },
    {
        "key": "4",
        "label": "Create New Companion Dirs",
        "tag": "CREATE",
        "tag_color": C.MAG,
        "color": C.WHT,
    },
    {
        "key": "99",
        "label": "Reset / Clear Alcove Directory",
        "tag": "DANGER",
        "tag_color": C.RED,
        "color": C.RED,
    },
]


def draw_banner():
    banner = r"""
 ╔══════════════════════════════════════════════════════════╗
 ║                                                          ║
 ║    █████   ██       ██████   ██████  ██     ██  ███████  ║
 ║   ██   ██  ██      ██       ██    ██  ██   ██   ██       ║
 ║   ███████  ██      ██       ██    ██   ██ ██    █████    ║
 ║   ██   ██  ██      ██       ██    ██    ███     ██       ║
 ║   ██   ██  ███████  ██████   ██████      █      ███████  ║
 ║                                                          ║
 ║               System Administration Wizard               ║
 ║                                                          ║
 ╚══════════════════════════════════════════════════════════╝
"""
    if _ENABLED:
        # Strip leading/trailing newlines to avoid printing extra empty lines
        lines = banner.strip("\n").splitlines()
        for line in lines:
            if "╔" in line or "╚" in line:
                # Top or bottom border
                print(s(C.DIM + C.BLU, line))
            elif line.startswith(" ║") and line.endswith("║"):
                left_border = s(C.DIM + C.BLU, " ║")
                right_border = s(C.DIM + C.BLU, "║")
                middle = line[2:-1]

                if "System Administration" in middle:
                    # Highlight the subtitle in bright yellow + italic
                    sub = "System Administration Wizard"
                    colored_sub = s(C.BYEL + C.ITA, sub)
                    middle_colored = middle.replace(sub, colored_sub)
                    print(f"{left_border}{middle_colored}{right_border}")
                elif middle.strip() == "":
                    # Empty padding line
                    print(f"{left_border}{middle}{right_border}")
                else:
                    # ASCII art line
                    print(f"{left_border}{s(C.BCYN, middle)}{right_border}")
            else:
                print(line)
    else:
        print(banner)


def draw_menu():
    draw_banner()

    tag_w = max(len(m["tag"]) for m in MENU_ITEMS)

    # Print main menu items (except 99)
    for m in MENU_ITEMS:
        if m["key"] == "99":
            continue
        tag = m["tag"].ljust(tag_w)
        key = m["key"].rjust(2)
        if _ENABLED:
            colored_tag = s(m["tag_color"], f" {tag} ")
            colored_key = s(C.BWHT, key)
            colored_label = s(m["color"], m["label"])
            print(f"  {colored_key}  {colored_tag}  {colored_label}")
        else:
            print(f"  {key}  [{tag}]  {m["label"]}")

    # Option 0: Exit Wizard
    print()
    exit_key = s(C.BWHT, " 0") if _ENABLED else " 0"
    exit_label = s(C.DIM, "Exit Wizard") if _ENABLED else "Exit Wizard"
    print(f"  {exit_key}   {" " * tag_w}   {exit_label}")

    # Option 99: Reset / Clear Alcove Directory (two options down from 0)
    print()
    print()
    for m in MENU_ITEMS:
        if m["key"] == "99":
            tag = m["tag"].ljust(tag_w)
            key = m["key"].rjust(2)
            if _ENABLED:
                colored_tag = s(m["tag_color"], f" {tag} ")
                colored_key = s(C.BWHT, key)
                colored_label = s(m["color"], m["label"])
                print(f"  {colored_key}  {colored_tag}  {colored_label}")
            else:
                print(f"  {key}  [{tag}]  {m["label"]}")

    print()

    line = s(C.DIM, "  " + "─" * (W + 4)) if _ENABLED else "  " + "─" * (W + 4)
    print(line)
    print()


def _noop_sigint(signum, frame):
    pass


def run_script(script_path, title):
    clear_screen()
    if _ENABLED:
        print(s(C.DIM, "┌" + "─" * (W + 2) + "┐"))
        print(s(C.DIM, "│") + s(C.BWHT, f"  Running: {title}".ljust(W + 2)) + s(C.DIM, "│"))
        print(s(C.DIM, "└" + "─" * (W + 2) + "┘"))
    else:
        print("┌" + "─" * (W + 2) + "┐")
        print("│" + f"  Running: {title}".ljust(W + 2) + "│")
        print("└" + "─" * (W + 2) + "┘")
    print()

    abs_path = os.path.join(_PROJECT_ROOT, script_path)
    if not os.path.isfile(abs_path):
        print(s(C.BRED, f"  ✖ Error: Script not found at {script_path}"))
        print()
        input(s(C.DIM, "  Press ENTER to return to the menu..."))
        return

    try:
        old_handler = signal.signal(signal.SIGINT, _noop_sigint)
        try:
            subprocess.run([sys.executable, abs_path], check=False)
        finally:
            signal.signal(signal.SIGINT, old_handler)
    except Exception as e:
        print(s(C.BRED, f"  ✖ Error running script: {e}"))

    print()
    if _ENABLED:
        print(s(C.DIM, "  " + "─" * (W + 2)))
    else:
        print("  " + "─" * (W + 2))
    input(s(C.DIM, "  Press ENTER to return to the menu..."))


def placeholder(title):
    clear_screen()
    if _ENABLED:
        print(s(C.DIM, "┌" + "─" * (W + 2) + "┐"))
        print(s(C.DIM, "│") + s(C.BWHT, f"  {title}".ljust(W + 2)) + s(C.DIM, "│"))
        print(s(C.DIM, "└" + "─" * (W + 2) + "┘"))
    else:
        print("┌" + "─" * (W + 2) + "┐")
        print("│" + f"  {title}".ljust(W + 2) + "│")
        print("└" + "─" * (W + 2) + "┘")
    print()
    if _ENABLED:
        wrapped = textwrap.wrap(
            "This option is currently a placeholder and will be added in a future update.",
            width=W - 2,
        )
        for line in wrapped:
            print(s(C.DIM, f"  {line}"))
    else:
        print("  This option is currently a placeholder and will be added")
        print("  in a future update.")
    print()
    if _ENABLED:
        print(s(C.DIM, "  " + "─" * (W + 2)))
    else:
        print("  " + "─" * (W + 2))
    input(s(C.DIM, "  Press ENTER to return to the menu..."))


def main():
    while True:
        clear_screen()
        draw_menu()

        try:
            prompt = s(C.BWHT + C.BLU, "  ➜ ") if _ENABLED else "  ➜ "
            choice = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n" + s(C.BGRN, "Exiting. Goodbye!"))
            sys.exit(0)

        if choice == "0":
            print("\n" + s(C.BGRN, "Exiting. Goodbye!"))
            sys.exit(0)
        elif choice == "1":
            run_script("utils/setup.py", "Generate / Configure config.py")
        elif choice == "2":
            run_script("utils/upgrade.py", "Migrate / Upgrade Installation")
        elif choice == "3":
            run_script("utils/install_deps.py", "Install / Update Dependencies")
        elif choice == "4":
            run_script("utils/create_new_companion.py", "Create New Companion Dirs")
        elif choice == "99":
            run_script("utils/cleanup.py", "Reset / Clear Directory")
        elif choice == "":
            continue
        else:
            print("\n  " + s(C.BRED, f"✖ Invalid option: {choice!r}"))
            print(s(C.DIM, "  Please enter a valid number from the menu."))
            print()
            input(s(C.DIM, "  Press ENTER to return to the menu..."))


if __name__ == "__main__":
    main()
