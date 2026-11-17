"""
Assembles the public site folder (_site) and refreshes live scores. Runs inside the GitHub Actions
workflow (.github/workflows/live.yml) every few minutes during tournament days, and on every push.

  * copies ONLY whitelisted public files into _site (nothing else can ever be published)
  * if lineups/current.json exists, scores the event with engine.live_score and writes _site/live_status.json
    through public_view(): before lineups lock the file contains no entrants/golfers at all
  * if scoring fails (ESPN hiccup), carries forward the last published live_status.json unchanged, so the site
    keeps showing the last good data and the header chip honestly turns to "Scores delayed" after 30 minutes

Pure standard library: the workflow needs no pip install.
"""
import json
import os
import shutil
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_URL = "https://emptysea215.github.io/3pc-standings/"

# the complete list of files that belong on the public site
PUBLIC_FILES = ["index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png",
                "icon-maskable-512.png", "apple-touch-icon.png", "og-image.png", ".nojekyll"]
LINEUPS = os.path.join("lineups", "current.json")


def copy_public_files(root, site):
    if os.path.isdir(site):
        shutil.rmtree(site)
    os.makedirs(site)
    for name in PUBLIC_FILES:
        src = os.path.join(root, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(site, name))
        elif name not in (".nojekyll",):
            print(f"WARNING: expected public file missing: {name}")


def carry_forward(site, fetch=None):
    """Keep the last published live_status.json when scoring fails. Returns True if one was kept."""
    fetch = fetch or (lambda: urllib.request.urlopen(SITE_URL + "live_status.json", timeout=20).read())
    try:
        data = fetch()
        json.loads(data)                      # only carry forward something that parses
        with open(os.path.join(site, "live_status.json"), "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"no previous live_status.json to carry forward ({type(e).__name__}: {e})")
        return False


def score(root, site, run=None):
    """Runs the live scorer; returns its exit code (0 = ok, including 'not started yet')."""
    lineups = os.path.join(root, LINEUPS)
    if not os.path.exists(lineups):
        print("no lineups/current.json: nothing to score (off week)")
        return 0
    if run is None:
        sys.path.insert(0, root)
        from engine.live_score import main as live_main
        run = live_main
    private = os.path.join(root, "_private", "live_status_full.json")   # never copied to _site
    return run(["--lineups", lineups, "--out", private,
                "--public-out", os.path.join(site, "live_status.json"),
                "--log", os.path.join(root, "_private", "live_update.log")])


def main(root=ROOT, run=None, fetch=None):
    site = os.path.join(root, "_site")
    copy_public_files(root, site)
    code = score(root, site, run)
    live = os.path.join(site, "live_status.json")
    if code != 0 and not os.path.exists(live):
        print("scoring failed: trying to keep the previously published live data")
        carry_forward(site, fetch)
    files = sorted(os.listdir(site))
    print("site assembled:", ", ".join(files))
    return 0   # never fail the deploy: an old-but-working site beats no site


if __name__ == "__main__":
    sys.exit(main())
