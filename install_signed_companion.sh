#!/usr/bin/env bash
# Install a Mozilla-signed companion XPI on bhbserv and the laptop.
# Usage: ./install_signed_companion.sh path/to/streamkeeper-0.2.3.xpi [laptop-ssh-host]
# Updates the Firefox enterprise policy install_url and autoplay allowlist.
# Firefox picks up the new add-on on its next restart.
set -euo pipefail
XPI=${1:?signed XPI path}; LAPTOP=${2:-10.0.0.121}
VERSION=$(unzip -p "$XPI" manifest.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')
unzip -l "$XPI" | grep -q 'META-INF/mozilla.rsa' || { echo "Not a Mozilla-signed XPI" >&2; exit 1; }
NAME="streamkeeper-$VERSION.xpi"
read -r -d '' POLICY <<EOF || true
import json,sys
p='/etc/firefox/policies/policies.json'
d=json.load(open(p))
d['policies']['ExtensionSettings']['streamkeeper@bhackerb.local']['install_url']='file://'+sys.argv[1]
allow=d['policies'].setdefault('Permissions',{}).setdefault('Autoplay',{}).setdefault('Allow',[])
for origin in ('https://streamed.pk','https://dailywrestling.cc'):
    if origin not in allow: allow.append(origin)
json.dump(d,open(p,'w'),indent=2)
print('policy ->',sys.argv[1])
EOF
mkdir -p "$HOME/.local/share/StreamKeeper"
cp "$XPI" "$HOME/.local/share/StreamKeeper/$NAME"
sudo cp /etc/firefox/policies/policies.json "/etc/firefox/policies/policies.json.before-streamkeeper-$VERSION-$(date +%s)"
sudo python3 -c "$POLICY" "$HOME/.local/share/StreamKeeper/$NAME"
ssh "$LAPTOP" mkdir -p .local/share/StreamKeeper
scp -q "$XPI" "$LAPTOP:.local/share/StreamKeeper/$NAME"
ssh "$LAPTOP" bash -s -- "$VERSION" "$NAME" <<REMOTE
sudo cp /etc/firefox/policies/policies.json /etc/firefox/policies/policies.json.before-streamkeeper-\$1-\$(date +%s)
sudo python3 -c $(printf %q "$POLICY") \$HOME/.local/share/StreamKeeper/\$2
REMOTE
echo "Installed $NAME on both hosts. Restart Firefox on each to load it (StreamKeeper reattaches owned tabs)."
