#!/bin/sh
# tests/fake_osascript.sh -- records the notification instead of showing one.
set -u
echo "$*" >> "${VP_FAKE_OSA_LOG:-/dev/null}"
exit 0
