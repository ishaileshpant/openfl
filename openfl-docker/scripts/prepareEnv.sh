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
# create a bridge network
docker network create openfl_network
docker run --rm \
  -e HOST_FQDN=$(hostname -f) \
  -v "$WORKSPACE_DIR":/home/openfl/workspace \
  -v "$VENV_DIR":/home/openfl/workspace_venv \
  openfl:latest \
  bash -c '
    echo $HOST_FQDN
    #python3 -m pip install virtualenv
    #python3 -m virtualenv ~/workspace_venv/venv
    #source ~/workspace_venv/venv/bin/activate
    fx workspace create --template torch_cnn_mnist --prefix workspace
    cd ~/workspace
    fx plan initialize -a $HOST_FQDN
    fx workspace certify
    fx aggregator generate-cert-request --fqdn $HOST_FQDN
    fx aggregator certify --silent --fqdn $HOST_FQDN
    #fx workspace export
  '
# Step 2: Setup collaborator 1
docker run --rm \
  -v "$WORKSPACE_DIR":/home/openfl/workspace \
  -v "$VENV_DIR":/home/openfl/workspace_venv \
  openfl:latest \
  bash -c "
    #python3 -m pip install virtualenv
    cd ~/workspace
    #source ~/workspace_venv/venv/bin/activate
    fx collaborator create -n collaborator1 -d 1
    fx collaborator generate-cert-request -n collaborator1
    fx collaborator certify -n collaborator1 --silent
  "

# Setup Collaborator 2

docker run --rm \
  -v "$WORKSPACE_DIR":/home/openfl/workspace \
  -v "$VENV_DIR":/home/openfl/workspace_venv \
  openfl:latest \
  bash -c "
    #python3 -m pip install virtualenv
    cd ~/workspace
    #source ~/workspace_venv/venv/bin/activate
    fx collaborator create -n collaborator2 -d 2
    fx collaborator generate-cert-request -n collaborator2
    fx collaborator certify -n collaborator2 --silent
  "

# Export the workspace
docker run --rm \
  -v "$WORKSPACE_DIR":/home/openfl/workspace \
  -v "$VENV_DIR":/home/openfl/workspace_venv \
  openfl:latest \
  bash -c "
    #python3 -m pip install virtualenv
    #source ~/workspace_venv/venv/bin/activate
    cd ~/workspace
    fx workspace export
  "
echo "Congratulations! workspace created and env prepared"
