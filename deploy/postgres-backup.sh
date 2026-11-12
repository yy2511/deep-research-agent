#!/bin/sh
set -eu
umask 077

deploy_root=${DRA_DEPLOY_ROOT:-/home/deploy/apps/deep-research-agent}
backup_path="$deploy_root/backups/postgres-$(date -u +%Y%m%dT%H%M%SZ).dump"

docker compose --file "$deploy_root/database/compose.yaml" exec -T postgres \
    sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_path.tmp"
mv "$backup_path.tmp" "$backup_path"
