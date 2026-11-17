#!/bin/sh
# Every apply updates the clean main clone, builds overlays, and maintains the
# harness views. Failed pulls keep the current source; failed builds keep the
# previous output. A sync failure must not stop the rest of chezmoi apply.
if ! "$HOME/.local/bin/skills" sync; then
    echo "update-skills: skills sync failed; keeping the previous skills installation" >&2
fi
