#!/bin/bash
i=0
while true; do
    echo "Count: $i  [$(date +%H:%M:%S)]"
    i=$((i + 1))
    sleep 2
done
