#!/bin/sh
# Plan 073: fdroid-index sidecar loop. Runs as root inside the official
# fdroidserver image (its /home/vagrant is 0700, so no other uid can run
# `fdroid`). Owns config.yml + keystore.p12; feather (uid FEATHER_UID,
# default 999) owns repo/ and metadata/ and only ever reads the index.
set -eu
: "${FDROID_KEYSTORE_PASSWORD:?FDROID_KEYSTORE_PASSWORD is required (the repo signing key password)}"
INTERVAL="${FDROID_UPDATE_INTERVAL:-15}"
FEATHER_UID="${FEATHER_UID:-999}"
cd /repo
. /etc/profile.d/bsenv.sh
FDROID="$fdroidserver/fdroid"

mkdir -p repo metadata
if [ ! -f keystore.p12 ]; then
  keytool -genkeypair -keystore keystore.p12 -storetype PKCS12 -alias feather \
    -keyalg RSA -keysize 4096 -validity 10000 -storepass "$FDROID_KEYSTORE_PASSWORD" \
    -dname "CN=feather" >/dev/null
  chmod 600 keystore.p12
fi
keytool -exportcert -keystore keystore.p12 -alias feather -storepass "$FDROID_KEYSTORE_PASSWORD" 2>/dev/null \
  | sha256sum | cut -d' ' -f1 > fingerprint.txt.tmp && mv fingerprint.txt.tmp fingerprint.txt

render_config() {
  # repo-config.json is written by feather: {"name","description","repo_url"}
  python3 - <<'PY'
import json, os
cfg = {}
try:
    cfg = json.load(open('repo-config.json'))
except Exception:
    pass
def q(s):  # single-quoted YAML scalar
    return "'" + str(s).replace("'", "''") + "'"
lines = [
  'repo_url: ' + q(cfg.get('repo_url') or os.environ.get('FDROID_REPO_URL') or 'http://localhost:7000/fdroid/repo'),
  'repo_name: ' + q(cfg.get('name') or 'Feather Android'),
  'repo_description: ' + q(cfg.get('description') or ''),
  'repo_icon: icon.png',
  'archive_older: 0',
  'keystore: keystore.p12',
  'repo_keyalias: feather',
  'keystorepass: {env: FDROID_KEYSTORE_PASSWORD}',
  'keypass: {env: FDROID_KEYSTORE_PASSWORD}',
  'keydname: CN=feather',
]
open('config.yml.tmp', 'w').write('\n'.join(lines) + '\n')
os.replace('config.yml.tmp', 'config.yml')
PY
  chmod 600 config.yml
}

run_update() {
  rm -f .update-requested            # consume BEFORE running so a request during the run is not lost
  render_config
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if "$FDROID" update --create-metadata --pretty > /tmp/fdroid-update.log 2>&1; then OK=true; else OK=false; fi
  # hand the outputs back to feather's uid; keystore/config stay root-only
  chown -R "$FEATHER_UID:$FEATHER_UID" repo metadata fingerprint.txt
  python3 - "$OK" "$START" <<'PY'
import json, sys, os, collections
ok = sys.argv[1] == 'true'
tail = collections.deque(open('/tmp/fdroid-update.log', errors='replace'), maxlen=40)
json.dump({'ok': ok, 'started_at': sys.argv[2], 'finished_at': __import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
           'log_tail': ''.join(tail)}, open('last-update.json.tmp', 'w'))
os.replace('last-update.json.tmp', 'last-update.json')
PY
  chown "$FEATHER_UID:$FEATHER_UID" last-update.json
  tail -n 3 /tmp/fdroid-update.log
}

run_update                            # always rebuild once at start-up
while true; do
  if [ -f .update-requested ]; then run_update; fi
  sleep "$INTERVAL"
done
