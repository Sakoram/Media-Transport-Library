#!/bin/bash

set -e

echo "Change P interface"
sed -i 's/0000:af:00.0/0000:86:00.0/g' *.json
echo "Change R interface"
sed -i 's/0000:af:00.1/0000:86:00.1/g' *.json