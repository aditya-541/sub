#!/bin/sh
set -eu

image="${SUBTENSOR_LOCALNET_IMAGE:-ghcr.io/raofoundation/subtensor-localnet:devnet}"
exec docker run --rm --name subnet-factory-localnet \
  -p 9944:9944 -p 9945:9945 \
  "$image"
