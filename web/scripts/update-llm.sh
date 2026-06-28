#!/bin/sh
# Daily cron: fetch LLM API token usage and update the database.
set -eu
: "${LLM_API_AUTH:?LLM_API_AUTH env var required}"
: "${LLM_DAYS:=1}"
: "${DB_PATH:=/opt/db-data/usage.sqlite}"
python3 /opt/dbtools/collect_llm.py --days "$LLM_DAYS" --db "$DB_PATH"
