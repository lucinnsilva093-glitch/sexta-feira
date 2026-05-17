#!/bin/bash
set -e

PORT=${PORT:-5000}
WORKERS=${GUNICORN_WORKERS:-2}

echo "Starting gunicorn on port $PORT with $WORKERS workers"
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers "$WORKERS" \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    app:app
