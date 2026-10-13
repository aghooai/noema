#!/bin/bash
# sha256 over the sorted per-file sha256 of src deploy tools (no __pycache__), first 16 hex: the E2/E3 QA lead's method
cd /codes/aghooai/noema && find src deploy tools -type f -not -path '*__pycache__*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16
