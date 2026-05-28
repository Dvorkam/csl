#!/bin/bash
echo "=== System Info ==="
uname -a
echo ""
echo "Repeating '${CSL_PARAM_MESSAGE}' x${CSL_PARAM_REPEAT}:"
for i in $(seq 1 "${CSL_PARAM_REPEAT}"); do
    echo "  $i: ${CSL_PARAM_MESSAGE}"
done
