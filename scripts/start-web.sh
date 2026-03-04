#!/bin/bash
#
# Start the Logging Web Interface
#
# This script starts both the API server and the web frontend.
# The API server runs on port 3001 and the web server on port 3002.
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Starting Logging Web Interface...${NC}"
echo ""

# Check if web dependencies are installed
if [ ! -d "$PLUGIN_DIR/web/node_modules" ]; then
    echo "Installing web dependencies..."
    cd "$PLUGIN_DIR/web"
    npm install
    echo ""
fi

# Start API server in background
echo -e "${GREEN}Starting API server on http://127.0.0.1:3001${NC}"
cd "$PLUGIN_DIR"
uv run python -m uvicorn api.server:app --host 127.0.0.1 --port 3001 &
API_PID=$!

# Give API server time to start
sleep 2

# Start web server in background
echo -e "${GREEN}Starting Web server on http://127.0.0.1:3002${NC}"
cd "$PLUGIN_DIR/web"
npm run dev &
WEB_PID=$!

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}🌐 Logging Web Interface is running!${NC}"
echo ""
echo "   API Server: http://127.0.0.1:3001"
echo "   Web Server: http://127.0.0.1:3002"
echo ""
echo "   Press Ctrl+C to stop both servers"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Handle shutdown
cleanup() {
    echo ""
    echo "Shutting down servers..."
    kill $API_PID 2>/dev/null
    kill $WEB_PID 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM

# Wait for either process to exit
wait $API_PID $WEB_PID
