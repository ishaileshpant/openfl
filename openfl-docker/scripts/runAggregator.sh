#!/bin/bash

HOST_UID=$(id -u)
HOST_GID=$(id -g)

# Set workspace directory
WORKSPACE_DIR=$(pwd)/my_workspace
VENV_DIR=$(pwd)/my_venv

# Create the workspace directory
mkdir -p "$WORKSPACE_DIR"
mkdir -p "$VENV_DIR"

sudo chown -R 1001:1001 $WORKSPACE_DIR $VENV_DIR
sudo chmod -R u+rwx $WORKSPACE_DIR $VENV_DIR

# Run the Aggregator
docker run -d --name aggregator \
  --network openfl_network \
  -v "$WORKSPACE_DIR":/home/openfl/workspace \
  -v "$VENV_DIR":/home/openfl/workspace_venv \
  -p 50051:50051 \
  -p 49840:49840 \
  openfl:latest \
  bash -c "
  #python3 -m pip install virtualenv
  #source ~/workspace_venv/venv/bin/activate
  cd ~/workspace
  pip install -r requirements.txt
  fx --log-level debug aggregator start
  "
echo "Congratulations! You've run your first federation with OpenFL"
