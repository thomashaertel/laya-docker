#!/bin/bash
# ---------------------------------------------------------------------------
# Laya entrypoint — grant the GPU device groups automatically.
#
# Docker resolves `group_add` names against the container's /etc/group, and
# the host's render GID differs between distros (992, 110, 105, ...).
# Instead of hardcoding it, read the GID straight from the device nodes that
# Docker passes through and start the app with those supplementary groups.
# Without GPU devices (CPU mode) the app starts unchanged.
# ---------------------------------------------------------------------------
set -e

dev_gids=$(stat -c %g /dev/kfd /dev/dri/renderD* 2>/dev/null | sort -un | tr '\n' ' ')

if [ -z "$dev_gids" ]; then
    echo "[entrypoint] no GPU devices found — starting in CPU mode"
    exec "$@"
fi

# Keep the groups the process already has (e.g. video from group_add).
groups=$(echo "$(id -G) $dev_gids" | tr ' ' '\n' | grep -v '^$' | sort -un | paste -sd,)

# Name the render group after the real host GID (logging only).
render_gid=$(stat -c %g /dev/dri/renderD* 2>/dev/null | head -1)
if [ -n "$render_gid" ]; then
    groupmod -o -g "$render_gid" render 2>/dev/null || true
fi

echo "[entrypoint] GPU device GIDs: ${dev_gids}-> supplementary groups: ${groups}"
exec setpriv --groups "$groups" -- "$@"
