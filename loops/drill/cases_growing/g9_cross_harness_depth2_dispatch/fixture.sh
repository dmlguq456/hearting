#!/bin/bash
# Deterministic full-chain positive contract; fake CLIs run, but no model turn does.
set -eu
WORK=$1
REPO="$WORK/repo"
mkdir -p "$REPO"
cd "$REPO"
git init -q
git checkout -q -b main
git config user.email drill@test
git config user.name drill
printf '# Strong cross-harness bounded parallel batch fixture\n' > README.md
git add README.md
git commit -q -m init
