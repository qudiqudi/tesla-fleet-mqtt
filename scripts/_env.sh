#!/usr/bin/env bash
# Sourced by the other scripts. Intended to run inside the tesla-tools container, where
# Dockhand injects all vars (incl. secrets) into the process env directly — no secret file,
# no docker inspect. Standalone users can instead provide a local .env.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# If a local env file exists (standalone use), load it. In the tools container there is none;
# the variables are already present in the environment.
for f in "$ROOT/.env.dockhand" "$ROOT/.env"; do
  if [ -f "$f" ]; then set -a; . "$f"; set +a; fi
done

need() { for v in "$@"; do [ -n "${!v:-}" ] || { echo "Missing $v in environment" >&2; exit 1; }; done; }
