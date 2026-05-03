#!/usr/local/bin/python3.13
import argparse
import sys
import logging
import os
import http.client
import json
import textwrap
import secrets
import socket
import string
import subprocess
import shlex
import random
import urllib.request

DEFAULT_API_HOST = "api.opalstack.com"
API_URL_ENV = os.environ.get("API_URL", f"https://{DEFAULT_API_HOST}")
API_HOST = API_URL_ENV.replace("https://", "").replace("http://", "").strip("/")
API_BASE_URI = "/api/v1"

CMD_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "UMASK": "0002",
}

PB_VERSION = "0.37.4"
PB_BINARY_URL = (
    f"https://github.com/pocketbase/pocketbase/releases/download/"
    f"v{PB_VERSION}/pocketbase_{PB_VERSION}_linux_amd64.zip"
)


# ---- Opalstack API wrapper ----
class OpalstackAPITool:
    """simple wrapper for http.client get and post"""

    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        if not authtoken:
            endpoint = self.base_uri + "/login/"
            payload = json.dumps({"username": user, "password": password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request(
                "POST",
                endpoint,
                payload,
                headers={"Content-type": "application/json"},
            )
            result = json.loads(conn.getresponse().read())
            if not result.get("token"):
                logging.warning("Invalid username or password and no auth token provided, exiting.")
                sys.exit(1)
            authtoken = result["token"]

        self.headers = {
            "Content-type": "application/json",
            "Authorization": f"Token {authtoken}",
        }

    def get(self, endpoint):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("GET", endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read())

    def post(self, endpoint, payload):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())


# ---- helpers ----
def ensure_dir(path, perms=0o700):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, perms)
    except PermissionError:
        pass


def create_file(path, contents, writemode="w", perms=0o600):
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f"Created file {path} with permissions {oct(perms)}")


def gen_password(length=32):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def download(url, localfile, perms=0o600):
    logging.info(f"Downloading {url} as {localfile}")
    req = urllib.request.Request(url, headers={"User-Agent": "opalstack-installer"})
    with urllib.request.urlopen(req) as r, open(localfile, "wb") as f:
        while True:
            data = r.read(8192)
            if not data:
                break
            f.write(data)
    os.chmod(localfile, perms)
    size = os.path.getsize(localfile)
    logging.info(f"Downloaded {url} as {localfile} ({size} bytes)")


def run_command(cmd, cwd=None, env=None):
    logging.info(f"Running: {cmd}")
    if env is None:
        env = CMD_ENV
    try:
        result = subprocess.check_output(
            shlex.split(cmd),
            cwd=cwd,
            env=env,
            stderr=subprocess.STDOUT,
        )
        return result
    except subprocess.CalledProcessError as e:
        logging.error(e.output.decode("utf-8", errors="ignore"))
        return e.output


def add_cronjob(cronjob_line):
    homedir = os.path.expanduser("~")
    tmpname = f"{homedir}/.tmp{gen_password(12)}"
    with open(tmpname, "w") as tmp:
        subprocess.run(["crontab", "-l"], stdout=tmp, stderr=subprocess.DEVNULL)
        tmp.write(f"{cronjob_line}\n")
    run_command(f"crontab {tmpname}")
    run_command(f"rm -f {tmpname}")
    logging.info(f"Added cron job: {cronjob_line}")


def main():
    parser = argparse.ArgumentParser(
        description="Installs PocketBase as an Opalstack userspace app (no systemctl)."
    )
    parser.add_argument("-i", dest="app_uuid", help="UUID of the base app",
                        default=os.environ.get("UUID"))
    parser.add_argument("-n", dest="app_name", help="name of the base app",
                        default=os.environ.get("APPNAME"))
    parser.add_argument("-t", dest="opal_token", help="API auth token",
                        default=os.environ.get("OPAL_TOKEN"))
    parser.add_argument("-u", dest="opal_user", help="Opalstack account name",
                        default=os.environ.get("OPAL_USER"))
    parser.add_argument("-p", dest="opal_password", help="Opalstack account password",
                        default=os.environ.get("OPAL_PASS"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )

    if not args.app_uuid:
        logging.error("Missing app UUID (-i or UUID env).")
        sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f"/app/read/{args.app_uuid}")

    osuser = appinfo["osuser_name"]
    appname = appinfo["name"]
    port = appinfo["port"]
    appdir = f"/home/{osuser}/apps/{appname}"
    logsdir = f"/home/{osuser}/logs/apps/{appname}"
    datadir = f"{appdir}/pb_data"

    logging.info(f"Installing PocketBase v{PB_VERSION} for app '{appname}' (user {osuser}, port {port})")

    ensure_dir(appdir, perms=0o700)
    ensure_dir(datadir, perms=0o700)
    ensure_dir(logsdir, perms=0o700)

    # ---- download + unzip binary ----
    zip_path = f"{appdir}/pocketbase.zip"
    download(PB_BINARY_URL, zip_path, perms=0o600)
    run_command(f"unzip -o {zip_path} -d {appdir}")
    run_command(f"rm -f {zip_path}")
    os.chmod(f"{appdir}/pocketbase", 0o700)

    # ---- start (idempotent; called by cron too) ----
    start_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail

        APPDIR="{appdir}"
        PIDFILE="$APPDIR/pocketbase.pid"
        LOGFILE="{logsdir}/pocketbase.log"
        PORT="{port}"

        if [ -f "$PIDFILE" ]; then
          PID="$(cat "$PIDFILE" || true)"
          if [ -n "$PID" ] && ps -p "$PID" >/dev/null 2>&1; then
            echo "PocketBase already running (PID $PID) on port $PORT"
            exit 0
          fi
          rm -f "$PIDFILE"
        fi

        cd "$APPDIR"
        nohup ./pocketbase serve --http="127.0.0.1:$PORT" --dir="$APPDIR/pb_data" >>"$LOGFILE" 2>&1 &
        NEW_PID=$!
        echo "$NEW_PID" > "$PIDFILE"
        echo "Started PocketBase (PID $NEW_PID) on port $PORT"
        """
    )
    create_file(f"{appdir}/start", start_script, perms=0o700)

    # ---- stop ----
    stop_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail

        APPDIR="{appdir}"
        PIDFILE="$APPDIR/pocketbase.pid"

        if [ ! -f "$PIDFILE" ]; then
          echo "No PID file found, nothing to stop for {appname}."
          exit 0
        fi

        PID="$(cat "$PIDFILE" || true)"
        if [ -z "$PID" ]; then
          rm -f "$PIDFILE"
          echo "Empty PID file; cleaned up."
          exit 0
        fi

        if ps -p "$PID" >/dev/null 2>&1; then
          kill "$PID" 2>/dev/null || true
          for _ in $(seq 1 20); do
            if ps -p "$PID" >/dev/null 2>&1; then
              sleep 0.5
            else
              break
            fi
          done
          if ps -p "$PID" >/dev/null 2>&1; then
            kill -9 "$PID" 2>/dev/null || true
          fi
          echo "Stopped PocketBase for {appname} (PID $PID)."
        else
          echo "Process with PID $PID not running for {appname}."
        fi

        rm -f "$PIDFILE"
        """
    )
    create_file(f"{appdir}/stop", stop_script, perms=0o700)

    # ---- cron watchdog (every 10 minutes, randomized minute) ----
    m = random.randint(0, 9)
    cron_line = f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1"
    add_cronjob(cron_line)

    # ---- create initial superuser before first start ----
    su_email = f"{osuser}@{socket.gethostname()}"
    su_pass = gen_password(24)
    run_command(
        f"{appdir}/pocketbase superuser upsert {su_email} {su_pass}",
        cwd=appdir,
    )

    # ---- start once ----
    run_command(f"{appdir}/start")

    msg = (
        f"PocketBase installed for app {appname} on 127.0.0.1:{port}. "
        f"Initial superuser: {su_email}, password: {su_pass}"
    )

    # ---- mark app installed + push dashboard notice ----
    api.post("/app/installed/", json.dumps([{"id": args.app_uuid}]))
    api.post("/notice/create/", json.dumps([{"type": "D", "content": msg}]))

    logging.info(f"Completed installation of PocketBase app {appname} - {msg}")


if __name__ == "__main__":
    main()
