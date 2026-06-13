# SPDX-License-Identifier: AGPL-3.0-or-later
#
# control-station-lite
# Copyright (C) 2026 Michal Dvořák
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version, with an additional permission for
# distribution through app stores (see LICENSE).

# Start a llama.cpp server (llama-server) as a persistent job. Parameters arrive
# as CSL_PARAM_* environment variables. The process runs in the foreground so
# the agent supervises it and can stream its output and kill it.
#
# Requires `llama-server` on PATH (llama.cpp build) and a GGUF model on the target.
$ErrorActionPreference = 'Stop'

$model = $env:CSL_PARAM_MODEL_PATH
if (-not $model) {
    Write-Error 'start_llama_server: model_path parameter is required'
    exit 1
}
if (-not (Test-Path -LiteralPath $model)) {
    Write-Error "start_llama_server: model file not found: $model"
    exit 1
}

$ctx = if ($env:CSL_PARAM_CONTEXT_SIZE) { $env:CSL_PARAM_CONTEXT_SIZE } else { '4096' }
$gpu = if ($env:CSL_PARAM_GPU_LAYERS) { $env:CSL_PARAM_GPU_LAYERS } else { '0' }

# Foreground exec — keeps the agent-supervised process alive for its lifetime.
& llama-server `
    --model $model `
    --ctx-size $ctx `
    --n-gpu-layers $gpu `
    --host 127.0.0.1 `
    --port 8080
