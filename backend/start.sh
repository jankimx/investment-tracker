#!/bin/bash
# Quick start script for investment tracker backend
# Run this from the backend/ directory after setting up .env

set -e

echo "=== Investment Tracker — Backend Setup ==="

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is required. Install from https://python.org"
    exit 1
fi

# Check .env exists
if [ ! -f .env ]; then
    echo "Creating .env from template..."
    cp .env.example .env
    echo ""
    echo ">>> IMPORTANT: Edit .env and add your MongoDB URI before continuing <<<"
    echo "    Open .env in any text editor and replace the MONGO_URI placeholder."
    echo ""
    read -p "Press Enter once you've updated .env..."
fi

# Install dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Start server
echo ""
echo "Starting Flask server on http://localhost:5000"
echo "Press Ctrl+C to stop."
echo ""
python app.py
