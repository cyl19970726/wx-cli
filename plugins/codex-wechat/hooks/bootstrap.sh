#!/bin/sh
set -eu

# Plugin activation does not imply private configuration or hook trust.
if [ -z "${WX_AGENT_PLUGIN_CONFIG:-}" ]; then
    exit 0
fi

exec "${WX_AGENT_PLUGIN_EXECUTABLE:-wx-agent-plugin}" --config "$WX_AGENT_PLUGIN_CONFIG" hook
