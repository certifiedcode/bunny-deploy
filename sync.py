#!/usr/bin/env python3
"""
Sync files to a Bunny Storage zone. Used by action.yml; see README.md.

Only one secret is needed: the Bunny account API key. The storage zone's
password and storage host are looked up through the Bunny API at run time
and masked in the logs.

Modes
  source mode (dir "."): uploads git-tracked files. On pushes only the files
    changed since the previous commit are uploaded/deleted and their URLs
    purged; manual runs (or full: "true") upload everything.
  build mode (any other dir): uploads every file in the folder. Files that
    disappear from the build are not deleted (use versioned file names).

Purging happens only on pushes: changed URLs in source mode, the whole pull
zone in build mode. Manual runs never purge.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API = os.environ.get("BUNNY_API_BASE", "https://api.bunny.net").rstrip("/")  # overridable for tests
SKIP_PREFIXES = (".github/", ".git/")
SKIP_FILES = {".gitignore", ".gitattributes"}
MAX_URL_PURGES = 300


def env(name, default=""):
    return os.environ.get(name, default).strip()


def fail(msg):
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


def http(method, url, headers, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read()
        return json.loads(body) if body and resp.headers.get_content_type() == "application/json" else None


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def wanted(path):
    return bool(path) and path not in SKIP_FILES and not path.startswith(SKIP_PREFIXES)


def quote(path):
    return "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))


def items(result):
    return result.get("Items", []) if isinstance(result, dict) else (result or [])


def find_storage_zone(api_key, name):
    q = urllib.parse.urlencode({"page": 0, "perPage": 1000, "search": name})
    for z in items(http("GET", f"{API}/storagezone?{q}", {"AccessKey": api_key, "Accept": "application/json"})):
        if z.get("Name") == name and not z.get("Deleted"):
            return z
    fail(f"Storage zone '{name}' not found for this API key")


def find_pull_zone_id(api_key, host):
    q = urllib.parse.urlencode({"page": 0, "perPage": 1000, "search": host.split(".")[0]})
    for z in items(http("GET", f"{API}/pullzone?{q}", {"AccessKey": api_key, "Accept": "application/json"})):
        if any(h.get("Value") == host for h in z.get("Hostnames") or []):
            return z["Id"]
    return None


def main():
    api_key = env("INPUT_API_KEY") or env("BUNNY_API_KEY")
    zone_name = env("INPUT_ZONE")
    public = env("INPUT_PUBLIC_URL").rstrip("/")
    folder = env("INPUT_DIR", ".") or "."
    full_input = env("INPUT_FULL", "auto").lower()
    event = env("EVENT_NAME")
    before = env("BEFORE_SHA")

    build_mode = os.path.normpath(folder) != "."
    no_parent = not before or set(before) == {"0"}
    full = build_mode or full_input == "true" or (full_input == "auto" and (event != "push" or no_parent))

    uploads, deletes = [], []
    if build_mode:
        if not os.path.isdir(folder):
            fail(f"Folder '{folder}' does not exist (did the build run?)")
        for root, _, files in os.walk(folder):
            for f in files:
                uploads.append(os.path.relpath(os.path.join(root, f), folder).replace(os.sep, "/"))
    elif full:
        uploads = [p for p in git("ls-files", "-z").split("\0") if wanted(p)]
    else:
        out = git("diff", "--name-status", "-z", "--no-renames", before, "HEAD")
        parts = [p for p in out.split("\0") if p]
        for status, path in zip(parts[0::2], parts[1::2]):
            if wanted(path):
                (deletes if status.startswith("D") else uploads).append(path)

    print(f"{'Full' if full else 'Incremental'} sync to '{zone_name}': {len(uploads)} upload(s), {len(deletes)} delete(s)", flush=True)
    if not uploads and not deletes:
        print("Nothing to deploy", flush=True)
        return
    if not api_key:
        fail("BUNNY_API_KEY is not set (add it as an organisation or repository secret)")
    if not zone_name:
        fail("storage-zone input is required")

    zone = find_storage_zone(api_key, zone_name)
    password = zone.get("Password") or fail("Storage zone has no password")
    print(f"::add-mask::{password}", flush=True)
    host = zone.get("StorageHostname") or "storage.bunnycdn.com"
    base = f"{host if '://' in host else 'https://' + host}/{urllib.parse.quote(zone_name)}/"
    auth = {"AccessKey": password}

    def upload(rel):
        with open(os.path.join(folder, rel), "rb") as f:
            http("PUT", base + quote(rel), {**auth, "Content-Type": "application/octet-stream"}, f.read())

    def delete(rel):
        try:
            http("DELETE", base + quote(rel), auth)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    failures = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        jobs = {pool.submit(upload, p): p for p in uploads}
        jobs.update({pool.submit(delete, p): p for p in deletes})
        for i, job in enumerate(as_completed(jobs), 1):
            try:
                job.result()
            except Exception as e:  # noqa: BLE001
                failures.append(f"{jobs[job]}: {e}")
            if i % 250 == 0:
                print(f"  {i}/{len(jobs)}", flush=True)
    if failures:
        print("\n".join(failures[:50]), file=sys.stderr)
        fail(f"{len(failures)} file(s) failed")

    # Purge: changed URLs after a push (source mode) or the whole pull zone
    # (build mode). A full source sync purges nothing, so a first upload doesn't
    # empty the live cache.
    headers = {"AccessKey": api_key}
    if event != "push":
        # Manual runs (first sync, repairs) never purge: the pull zone may still
        # serve another origin, and a purge then just reloads everything from it.
        print("Not purging on a manual run", flush=True)
    elif public and build_mode:
        # Build output has no reliable change list: purge the whole pull zone.
        pz = find_pull_zone_id(api_key, urllib.parse.urlparse(public).hostname or "")
        if pz:
            http("POST", f"{API}/pullzone/{pz}/purgeCache", headers)
            print(f"Purged pull zone {pz}", flush=True)
        else:
            print("::warning::No pull zone has the public-url hostname; nothing purged", flush=True)
    elif public and not full:
        changed = uploads + deletes
        if len(changed) <= MAX_URL_PURGES:
            for rel in changed:
                url = urllib.parse.quote(f"{public}/{quote(rel)}", safe="")
                http("POST", f"{API}/purge?url={url}&async=false", headers)
            print(f"Purged {len(changed)} URL(s)", flush=True)
        else:
            pz = find_pull_zone_id(api_key, urllib.parse.urlparse(public).hostname or "")
            if pz:
                http("POST", f"{API}/pullzone/{pz}/purgeCache", headers)
                print(f"Purged pull zone {pz}", flush=True)
            else:
                print("::warning::Too many changes to purge by URL and no pull zone matched public-url", flush=True)
    print("Done", flush=True)


if __name__ == "__main__":
    main()
