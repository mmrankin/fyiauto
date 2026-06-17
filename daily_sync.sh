#!/bin/bash
# fyiAuto nightly full sync — all vehicles + photos.
# Scheduled via crontab at 08:00 America/Chicago. Streams the whole
# tbl_inventory in keyset pages (memory-safe) and pulls photos in batches.
cd /Users/markrankin/fyiAuto || exit 1

export SYNC_PHOTOS=parsedimage   # pull real photos from tbl_parsedImage
export SYNC_BATCH=2000           # page size for decode/stream

echo "===== full sync started $(date) =====" >> sync.log
/Users/markrankin/fyiAuto/.venv/bin/python sync.py --full >> sync.log 2>&1
echo "===== full sync finished $(date) (exit $?) =====" >> sync.log
