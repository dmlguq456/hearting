#!/usr/bin/env bash
# Install only missing test tools. Runner package indexes normally suffice;
# a stale index or stalled Azure mirror gets one official Ubuntu retry.
set -euo pipefail

packages=()
commands=()
for package in "$@"; do
  case "$package" in
    ripgrep) command_name=rg ;;
    strace) command_name=strace ;;
    bubblewrap) command_name=bwrap ;;
    *) echo "Unknown CI fixture: $package" >&2; exit 2 ;;
  esac
  if ! command -v "$command_name" >/dev/null; then
    packages+=("$package")
    commands+=("$command_name")
  fi
done
if ((${#packages[@]} == 0)); then
  exit 0
fi

apt_options=(-o Acquire::Retries=2 -o Acquire::http::Timeout=15
             -o Acquire::https::Timeout=15 -o Acquire::Languages=none)
install_once() {
  if sudo timeout 90 apt-get "${apt_options[@]}" install --yes --no-install-recommends "${packages[@]}"; then
    return 0
  fi
  # Ignore unrelated third-party feeds and the runner's Azure mirror list.
  # Keep the usual Ubuntu components and signature verification.
  . /etc/os-release
  local sources
  sources=$(mktemp)
  printf 'deb https://archive.ubuntu.com/ubuntu %s main universe\n' \
    "$VERSION_CODENAME" "${VERSION_CODENAME}-updates" > "$sources"
  printf 'deb https://security.ubuntu.com/ubuntu %s-security main universe\n' \
    "$VERSION_CODENAME" >> "$sources"
  local official=("${apt_options[@]}" -o "Dir::Etc::sourcelist=$sources" -o Dir::Etc::sourceparts=-)
  local status=0
  sudo timeout 120 apt-get "${official[@]}" update &&
    sudo timeout 90 apt-get "${official[@]}" install --yes --no-install-recommends "${packages[@]}" ||
    status=$?
  rm -f "$sources"
  return "$status"
}
# A name-resolution or mirror outage on the runner (2026-11-11: nine runs lost the
# same hour to "Temporary failure resolving") often clears within a minute, so back
# off and try again here instead of failing the shard. Three rounds at most, and no
# new round once four minutes are spent, so a real stall still ends the step.
for round in 1 2 3; do
  if install_once; then
    break
  fi
  if ((round == 3 || SECONDS > 240)); then
    echo "Could not install ${packages[*]} after $round rounds" >&2
    exit 1
  fi
  echo "apt install failed (round $round); trying again in $((round * 15)) s" >&2
  sleep $((round * 15))
done
for command_name in "${commands[@]}"; do
  command -v "$command_name" >/dev/null
done
