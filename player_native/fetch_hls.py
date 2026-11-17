"""Fetch media3-exoplayer-hls from Google Maven and extract its classes.jar.

Chromium vendors androidx.media3 1.2.0 but not the HLS module. The AAR carries
only Java classes (no resources, no native code), so the jar alone is enough
and a plain java_import can consume it. The version is pinned to the vendored
media3 version; bump both together.
"""

import hashlib
import io
import os
import sys
import urllib.request
import zipfile

VERSION = "1.2.0"
BASE = "https://dl.google.com/dl/android/maven2/androidx/media3/media3-exoplayer-hls"
URL = f"{BASE}/{VERSION}/media3-exoplayer-hls-{VERSION}.aar"


def main():
    out = sys.argv[1]
    aar = urllib.request.urlopen(URL, timeout=120).read()
    expected = urllib.request.urlopen(URL + ".sha1", timeout=60).read().decode().strip()
    actual = hashlib.sha1(aar).hexdigest()
    assert actual == expected, f"media3-exoplayer-hls sha1 mismatch: {actual} != {expected}"
    classes = zipfile.ZipFile(io.BytesIO(aar)).read("classes.jar")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        f.write(classes)
    print(f"player: media3-exoplayer-hls {VERSION} ({len(classes)} bytes) -> {out}")


if __name__ == "__main__":
    main()