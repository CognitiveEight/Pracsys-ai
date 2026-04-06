#!/bin/bash

# SimplerToday Startup Script

echo "=========================================="
echo "SimplerToday - Starting Application"
echo "=========================================="

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install/Update dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "WARNING: .env file not found!"
    echo "Please create .env file from env.example"
    echo "cp env.example .env"
    exit 1
fi

# Create necessary directories
echo "Creating directories..."
mkdir -p logs
mkdir -p logs/audit
mkdir -p data/workspaces

# Start the application
echo "Starting application..."
echo "=========================================="
python -m app.main

# Deactivate virtual environment on exit
deactivate

