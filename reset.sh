#!/bin/bash
# Restore the demo repo to its broken state. Run this between live demos.
cd "$(dirname "$0")"
rm -rf scratchrepo && cp -R scratchrepo.pristine scratchrepo
cd scratchrepo && python3 -m pytest -q 2>&1 | tail -1
