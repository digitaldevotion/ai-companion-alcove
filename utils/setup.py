#!/usr/bin/env python3
# ============================================
# Alcove — Configuration Setup Wizard
# ============================================

import os
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None


# ── Auto-activate ~/alcove-env (macOS / Linux only) ─────────────────
if os.name != "nt" and not os.environ.get("VIRTUAL_ENV") and sys.prefix == sys.base_prefix:
    _venv_dir = os.path.expanduser("~/alcove-env")
    _venv_python = os.path.join(_venv_dir, "bin", "python3")
    if os.path.isdir(_venv_dir) and os.path.isfile(_venv_python):
        os.environ["VIRTUAL_ENV"] = _venv_dir
        os.environ["PATH"] = os.path.join(_venv_dir, "bin") + os.pathsep + os.environ.get("PATH", "")
        os.execv(_venv_python, [_venv_python] + sys.argv)


class C:
    """ANSI color / style constants."""
    RST = "\033[0m"
    BLD = "\033[1m"
    DIM = "\033[2m"
    ITA = "\033[3m"
    UND = "\033[4m"
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


def ask_question(prompt_text):
    val = input(prompt_text).strip()
    print()  # Extra return between questions
    return val


def ask_question_with_default(prompt_text, default_value):
    print(prompt_text.rstrip(" :") + ":")
    if readline and hasattr(readline, "set_startup_hook"):
        readline.set_startup_hook(lambda: readline.insert_text(default_value))
        try:
            val = input("> ").strip()
        finally:
            readline.set_startup_hook()
    else:
        # Fallback if readline is not supported/available
        val = input(f"> [{default_value}]: ").strip()
    
    if not val:
        val = default_value
    print()  # Extra return between questions
    return val


def _run_setup():
    # Determine paths
    utils_dir = Path(__file__).parent.resolve()
    root_dir = utils_dir.parent.resolve()
    template_path = utils_dir / "config_template.py"
    config_path = root_dir / "config.py"

    if not template_path.exists():
        print(s(C.BRED, f"❌ Error: Config template not found at: {template_path}"))
        return 1

    template_content = template_path.read_text(encoding="utf-8")
    _tmpl_text_model_match = re.search(r'CURRENT_TEXT_MODEL\s*=\s*"([^"]*)"', template_content)
    _tmpl_text_model = _tmpl_text_model_match.group(1) if _tmpl_text_model_match else "z-ai/glm-5.1"
    _tmpl_image_model_match = re.search(r'CURRENT_IMAGE_MODEL\s*=\s*"([^"]*)"', template_content)
    _tmpl_image_model = _tmpl_image_model_match.group(1) if _tmpl_image_model_match else "google/gemini-3.1-flash-image-preview"

    while True:
        clear_screen()
        print(s(C.BCYN + C.BLD, "=========================================="))
        print(s(C.BCYN + C.BLD, "         Alcove Configuration Wizard"))
        print(s(C.BCYN + C.BLD, "=========================================="))
        print()
        print("This wizard will guide you through setting up your 'config.py'.")
        print("Please enter the requested information below.")
        print()

        # 0. Timezone offset (UTC offset)
        while True:
            timezone_offset = ask_question("Enter your local time zone's UTC offset in hours\n(e.g., -5 for EST, +1 for CET, +5.5 for IST, -3.5 for Newfoundland): ")
            try:
                tz_val = float(timezone_offset)
                if -12 <= tz_val <= 14:
                    break
                print(s(C.BRED, "Error: UTC offset must be between -12 and +14."))
            except ValueError:
                print(s(C.BRED, "Error: Please enter a valid number (e.g., -5, +5.5, 0)."))
            print()

        # 1. Discord token (required)
        while True:
            discord_token = ask_question("Enter your Discord token (required): ")
            if discord_token:
                break
            print(s(C.BRED, "Error: Discord token is required."))
            print()

        # 2 & 3. LLM Keys (at least one of OpenRouter or NanoGPT is required)
        while True:
            openrouter_key = ask_question("Enter your OpenRouter key if you wish to use OpenRouter\n(or press Enter to skip): ")
            nanogpt_key = ask_question("Enter your NanoGPT key if you wish to use NanoGPT\n(or press Enter to skip): ")
            
            if openrouter_key or nanogpt_key:
                break
            print(s(C.BRED, "Error: You must enter at least one key (OpenRouter or NanoGPT)."))
            print()

        # Determine default provider
        provider = "openrouter"
        if openrouter_key and nanogpt_key:
            while True:
                print("Both OpenRouter and NanoGPT keys were entered.")
                choice = input("Which one do you wish to use as the default provider? (openrouter/nanogpt): ").strip().lower()
                print()  # Extra return between questions
                if choice in ['openrouter', 'nanogpt']:
                    provider = choice
                    break
                else:
                    print(s(C.BRED, "Invalid provider. Please type 'openrouter' or 'nanogpt'."))
                    print()
        elif openrouter_key:
            provider = "openrouter"
        elif nanogpt_key:
            provider = "nanogpt"

        # Determine default models
        default_text_model = "zai-org/" + _tmpl_text_model[len("z-ai/"):] if provider == "nanogpt" and _tmpl_text_model.startswith("z-ai/") else _tmpl_text_model
        if provider == "openrouter":
            default_image_model = _tmpl_image_model
        else:
            default_image_model = "nano-banana-2"

        print(s(C.BCYN + C.BLD, "--- Text Model Selection ---"))
        print("To browse available text models, visit:")
        print(f"  NanoGPT    - {s(C.UND, 'https://nano-gpt.com/models/text')}")
        print(f"  OpenRouter - {s(C.UND, 'https://openrouter.ai/models?output_modalities=text')}")
        print()
        print(f"The starting default {s(C.BGRN, default_text_model)} for the text model.")
        print()
        current_text_model = ask_question_with_default(
            "Enter the default text model you wish to use or press ENTER to accept the default: ",
            default_text_model
        )

        print(s(C.BCYN + C.BLD, "--- Image Model Selection ---"))
        print("To browse available image models, visit:")
        print(f"  NanoGPT    - {s(C.UND, 'https://nano-gpt.com/models/image')}")
        print(f"  OpenRouter - {s(C.UND, 'https://openrouter.ai/models?output_modalities=image')}")
        print()
        print(f"The starting default {s(C.BGRN, default_image_model)}")
        print(f"for the image model on {provider.capitalize()}.")
        print()
        current_image_model = ask_question_with_default(
            "Enter the default image model you wish to use or press ENTER to accept the default: ",
            default_image_model
        )

        # 4. ElevenLabs API key
        elevenlabs_api_key = ask_question("Enter your ElevenLabs API key if you wish to use voice mode\n(or press Enter to skip): ")

        elevenlabs_voice_id = ""
        current_voice_text_model = current_text_model
        if elevenlabs_api_key:
            while True:
                elevenlabs_voice_id = ask_question("Enter the default voice ID you want to use with ElevenLabs (required): ")
                if elevenlabs_voice_id:
                    break
                print(s(C.BRED, "Error: Voice ID is required when using ElevenLabs."))
                print()

            print(s(C.BCYN + C.BLD, "--- Voice Text Model Selection ---"))
            print("This is the text model used to generate responses for voice mode.")
            print()
            print(f"The starting default is your text model: {s(C.BGRN, current_text_model)}")
            print()
            current_voice_text_model = ask_question_with_default(
                "Enter the voice text model you wish to use or press ENTER to accept the default: ",
                current_text_model
            )

        # Summary screen
        clear_screen()
        print(s(C.BCYN + C.BLD, "=========================================="))
        print(s(C.BCYN + C.BLD, "           Configuration Summary"))
        print(s(C.BCYN + C.BLD, "=========================================="))
        print()
        print(f"{'UTC Offset':<20}: {timezone_offset}")
        print(f"{'Discord Token':<20}: {discord_token}")
        print(f"{'OpenRouter Key':<20}: {openrouter_key if openrouter_key else '(empty)'}")
        print(f"{'NanoGPT Key':<20}: {nanogpt_key if nanogpt_key else '(empty)'}")
        print(f"{'Default Provider':<20}: {provider}")
        print(f"{'Text Model':<20}: {current_text_model}")
        print(f"{'Image Model':<20}: {current_image_model}")
        print(f"{'ElevenLabs API Key':<20}: {elevenlabs_api_key if elevenlabs_api_key else '(empty)'}")
        if elevenlabs_api_key:
            print(f"{'ElevenLabs Voice ID':<20}: {elevenlabs_voice_id if elevenlabs_voice_id else '(empty)'}")
            print(f"{'Voice Text Model':<20}: {current_voice_text_model}")
        print()

        confirm = input("Is everything correct? (y/n)? ").strip().lower()
        if confirm in ['y', 'yes']:
            break
        print("\nRestarting wizard...\n")

    print()
    if config_path.exists():
        print(s(C.BYEL + C.BLD, "⚠️ Warning: 'config.py' already exists at the target location."))
        overwrite = input("Do you wish to overwrite it? (y/n)? ").strip().lower()
        if overwrite not in ['y', 'yes']:
            print()
            print("Setup cancelled. Existing 'config.py' was not modified.")
            return 0

        # Backup the existing config.py
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_path = root_dir / f"config.py_{timestamp}"
            shutil.copy2(config_path, backup_path)
            print()
            print(f"Safe Backup: Created {s(C.BGRN, backup_path.name)}")
            print()
        except Exception as e:
            print()
            print(s(C.BYEL, f"⚠️ Warning: Could not create backup of 'config.py': {e}"))
            print()

    print("Generating your 'config.py' file...")

    try:
        content = template_content

        # Replace PROVIDER
        content = re.sub(
            r'(PROVIDER\s*=\s*os\.getenv\("PROVIDER",\s*")[^"]*("\))',
            rf'\g<1>{provider}\g<2>',
            content
        )

        # Replace DISCORD_TOKEN
        content = content.replace("REPLACE_WITH_DISCORD_TOKEN", discord_token)

        # Replace OPENROUTER_KEY
        content = content.replace("REPLACE_WITH_OPENROUTER_KEY", openrouter_key)

        # Replace NANOGPT_KEY
        content = re.sub(
            r'(NANOGPT_KEY\s*=\s*os\.getenv\("NANOGPT_KEY",\s*")[^"]*("\))',
            rf'\g<1>{nanogpt_key}\g<2>',
            content
        )

        # Replace ELEVENLABS_API_KEY
        content = content.replace("REPLACE_WITH_ELEVENLABS_API_KEY", elevenlabs_api_key)

        # Replace ELEVENLABS_VOICE_ID
        content = content.replace("REPLACE_WITH_ELEVENLABS_VOICE_ID", elevenlabs_voice_id)

        # Replace CURRENT_TEXT_MODEL
        content = re.sub(
            r'(CURRENT_TEXT_MODEL\s*=\s*")[^"]*(")',
            rf'\g<1>{current_text_model}\g<2>',
            content
        )

        # Replace CURRENT_IMAGE_MODEL
        content = re.sub(
            r'(CURRENT_IMAGE_MODEL\s*=\s*")[^"]*(")',
            rf'\g<1>{current_image_model}\g<2>',
            content
        )

        # Replace CURRENT_VOICE_TEXT_MODEL
        content = re.sub(
            r'(CURRENT_VOICE_TEXT_MODEL\s*=\s*")[^"]*(")',
            rf'\g<1>{current_voice_text_model}\g<2>',
            content
        )

        # Replace TIMEZONE_OFFSET
        content = re.sub(
            r'(TIMEZONE_OFFSET\s*=\s*)[^\s#]+',
            rf'\g<1>{timezone_offset}',
            content
        )

        # Write to alcove root
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)

        print()
        print(s(C.BGRN + C.BLD, "=========================================="))
        print(s(C.BGRN + C.BLD, "🎉 Success! config.py has been generated!"))
        print(s(C.BGRN + C.BLD, "=========================================="))
        print(f"File created at: {config_path}")
        print()
        return 0

    except Exception as e:
        print(s(C.BRED, f"❌ Error writing config.py: {e}"))
        return 1


def main():
    try:
        return _run_setup()
    except (KeyboardInterrupt, EOFError):
        print()
        print(s(C.BYEL + C.BLD, "⚠️ Setup cancelled by user. Existing configuration not modified."))
        print()
        return 1


if __name__ == "__main__":
    sys.exit(main())
