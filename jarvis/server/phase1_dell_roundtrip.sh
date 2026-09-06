#!/usr/bin/env bash
# Phase 1 Dell -> Lenovo brain verification helper.
# Run these commands from the Dell server (or any machine that can reach the Lenovo over Tailscale).
#
# Expected pre-requisites on the Dell:
#  - JARVIS_BRAIN_URL=http://100.102.49.30:8001  (in .env for the Dell server)
#  - Lenovo brain service running: python server/brain_service.py --host 0.0.0.0 --port 8001
#
# This file is a convenience reference, not part of the server runtime.

DLLEXPORT BRAIN_URL="http://100.102.49.30:8001"

echo "=== Dell -> Lenovo brain /healthz ==="
curl -s "$BRAIN_URL/healthz" | python -m json.tool

echo
echo "=== Dell -> Lenovo brain /v1/chat ==="
curl -s -X POST "$BRAIN_URL/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hello in one short sentence."}]}' \
  | python -m json.tool

echo
echo "=== Dell -> Lenovo brain invalid request (empty messages) ==="
curl -s -X POST "$BRAIN_URL/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{"messages":[]}' \
  | python -m json.tool
