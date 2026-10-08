# Custom Onyx backend with the Nextcloud connector.
#
# The base image is the unmodified upstream release; this only overlays the
# patched Python files. Build context: onyx-patch/backend/
#
# Build:  docker build -f onyx-patch/backend.Dockerfile \
#           -t onyx-nextcloud-backend:v4.9.0 onyx-patch/backend/
#
# Rollback: unset ONYX_BACKEND_IMAGE in the deployment .env and recreate.

FROM onyxdotapp/onyx-backend:v4.9.0

COPY onyx/ /app/onyx/

# Drop stale bytecode so the patched modules are recompiled.
RUN find /app/onyx -name '__pycache__' -type d -prune -exec rm -rf {} +
