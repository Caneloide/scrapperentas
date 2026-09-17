#!/usr/bin/env bash
# Salir si ocurre un error
set -o errexit

pip install -r requirements.txt
playwright install chromium
