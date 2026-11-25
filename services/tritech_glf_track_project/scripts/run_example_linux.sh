#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
python run_all.py \
  --glf /path/to/log_2025-06-19-203913.glf \
  --profile config/tanana_203913_profile.json \
  --with-association
