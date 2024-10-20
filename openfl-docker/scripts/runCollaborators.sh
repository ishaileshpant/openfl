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

# Step 1: Setup Federation Workspace & Certificate Authority (CA)

# Run Collaborator 1
docker run -d --name collaborator1 \
  --network openfl_network \
  -v "$WORKSPACE_DIR":/home/openfl/workspace \
  -v "$VENV_DIR":/home/openfl/workspace_venv \
  openfl:latest \
  bash -c "
  #python3 -m pip install virtualenv
  #source ~/workspace_venv/venv/bin/activate
  fx workspace import --archive ~/workspace/workspace.zip
  cd ~/workspace
  pip install -r requirements.txt
  fx collaborator certify --import agg_to_col_collaborator1_signed_cert.zip
  fx --log-level debug collaborator start -n collaborator1
  "

# Run Collaborator 2
docker run -d --name collaborator2 \
  --network openfl_network \
  -v "$WORKSPACE_DIR":/home/openfl/workspace \
  -v "$VENV_DIR":/home/openfl/workspace_venv \
  openfl:latest \
  bash -c "
  #python3 -m pip install virtualenv
  #source ~/workspace_venv/venv/bin/activate
  fx workspace import --archive ~/workspace/workspace.zip
  cd ~/workspace
  pip install -r requirements.txt
  fx collaborator certify --import agg_to_col_collaborator2_signed_cert.zip*
  fx --log-level debug collaborator start -n collaborator2
  "

  echo "Collaborator(s) are up and running"
