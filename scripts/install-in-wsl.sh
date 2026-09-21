#!/usr/bin/env bash
# Run as root inside the privasheet-dev distribution:
#   bash /tmp/nightshift-src/scripts/install-in-wsl.sh /tmp/nightshift-src
# Installs the orchestrator root-owned (the agent cannot change its own rules),
# creates the agent's state directory and config, the project venv and the repository clone.
set -euo pipefail

SRC="${1:?source directory}"
AGENT=agent
AGENT_HOME=/home/$AGENT
REPO_URL="${REPO_URL:-https://github.com/mekhal/PrivaSheet.git}"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "== orchestrator -> /opt/nightshift (root-owned)"
rm -rf /opt/nightshift
mkdir -p /opt/nightshift
python3 -m venv /opt/nightshift/venv
/opt/nightshift/venv/bin/pip install -q --no-deps "$SRC"
chown -R root:root /opt/nightshift
chmod -R go-w /opt/nightshift
cat > /usr/local/bin/nightshift <<'EOF'
#!/usr/bin/env bash
exec /opt/nightshift/venv/bin/python -m nightshift "$@"
EOF
chmod 755 /usr/local/bin/nightshift

echo "== state directory and config"
install -d -o $AGENT -g $AGENT "$AGENT_HOME/nightshift" "$AGENT_HOME/work"
if [[ ! -f "$AGENT_HOME/nightshift/config.toml" ]]; then
  install -o $AGENT -g $AGENT -m 644 "$SRC/config.example.toml" "$AGENT_HOME/nightshift/config.toml"
  echo "   created config.toml (mode = dry-run)"
else
  echo "   kept existing config.toml"
fi

echo "== project venv (gates) and repository"
sudo -u $AGENT -H bash -lc '
set -euo pipefail
cd ~/work
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip pytest ruff
if [[ ! -d PrivaSheet/.git ]]; then
  git clone -q "'"$REPO_URL"'" PrivaSheet
fi
cd PrivaSheet
git remote set-url --push origin DISABLED   # no pushing from the distribution
if git show-ref --verify --quiet refs/heads/develop; then
  :
elif git show-ref --verify --quiet refs/remotes/origin/develop; then
  git checkout -q -b develop origin/develop
else
  git checkout -q -b develop
fi
git checkout -q develop
echo "   repo on $(git rev-parse --abbrev-ref HEAD) at $(git rev-parse --short HEAD)"
'

echo "== done: $(nightshift --help | head -1)"
