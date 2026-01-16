#!/bin/bash

# SimplerToday Development Helper Script
# Provides quick access to common development tasks

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_header() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

# Check if virtual environment is activated
check_venv() {
    if [[ -z "$VIRTUAL_ENV" ]]; then
        if [[ -d "venv" ]]; then
            print_info "Activating virtual environment..."
            source venv/bin/activate
        else
            print_warning "Virtual environment not found. Creating..."
            python3 -m venv venv
            source venv/bin/activate
            pip install -q --upgrade pip
        fi
    fi
}

# Command functions
cmd_setup() {
    print_header "Setting Up SimplerToday"
    
    check_venv
    
    print_info "Installing dependencies..."
    pip install -q -r requirements.txt
    print_success "Dependencies installed"
    
    if [[ ! -f ".env" ]]; then
        print_info "Creating .env file..."
        cp env.example .env
        print_warning "Please edit .env with your configuration!"
    else
        print_success ".env file exists"
    fi
    
    print_info "Creating directories..."
    mkdir -p logs logs/audit data/workspaces
    print_success "Directories created"
    
    print_success "Setup complete!"
    print_info "Next: Edit .env, then run: $0 start"
}

cmd_start() {
    print_header "Starting SimplerToday"
    
    # Check if .env exists
    if [[ ! -f ".env" ]]; then
        print_error ".env file not found!"
        print_info "Run: $0 setup"
        exit 1
    fi
    
    check_venv
    
    print_info "Starting application..."
    python -m app.main
}

cmd_test() {
    print_header "Running Tests"
    
    check_venv
    
    python test_setup.py
}

cmd_docker_start() {
    print_header "Starting Docker Services"
    
    print_info "Starting Qdrant and Redis..."
    docker-compose up -d
    
    sleep 2
    
    print_success "Services started!"
    docker-compose ps
}

cmd_docker_stop() {
    print_header "Stopping Docker Services"
    
    docker-compose down
    print_success "Services stopped"
}

cmd_docker_logs() {
    print_header "Docker Service Logs"
    
    docker-compose logs -f
}

cmd_logs() {
    print_header "Application Logs"
    
    if [[ ! -d "logs" ]]; then
        print_error "No logs directory found"
        exit 1
    fi
    
    echo "Which log to view?"
    echo "1) Application (all)"
    echo "2) Errors only"
    echo "3) Audit"
    echo "4) Structured (JSON)"
    read -p "Choice [1-4]: " choice
    
    case $choice in
        1) tail -f logs/app.log ;;
        2) tail -f logs/errors.log ;;
        3) tail -f logs/audit/audit.log ;;
        4) tail -f logs/structured.log ;;
        *) print_error "Invalid choice" ;;
    esac
}

cmd_clean() {
    print_header "Cleaning Project"
    
    print_info "Removing Python cache..."
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete
    find . -type f -name "*.pyo" -delete
    
    print_info "Removing logs..."
    rm -rf logs/*.log 2>/dev/null || true
    
    print_success "Cleaned!"
}

cmd_status() {
    print_header "SimplerToday Status"
    
    # Check venv
    if [[ -d "venv" ]]; then
        print_success "Virtual environment exists"
    else
        print_error "Virtual environment not found"
    fi
    
    # Check .env
    if [[ -f ".env" ]]; then
        print_success ".env file exists"
    else
        print_error ".env file not found"
    fi
    
    # Check Docker services
    print_info "Docker services:"
    if command -v docker-compose &> /dev/null; then
        docker-compose ps 2>/dev/null || print_warning "Services not running"
    else
        print_warning "docker-compose not installed"
    fi
    
    # Check logs
    if [[ -d "logs" ]]; then
        log_count=$(find logs -type f -name "*.log" 2>/dev/null | wc -l)
        print_info "Log files: $log_count"
    fi
}

cmd_info() {
    print_header "SimplerToday Information"
    
    echo ""
    echo "📦 Project: SimplerToday"
    echo "📝 Version: 0.1.0"
    echo "🐍 Python: $(python --version 2>&1 | awk '{print $2}')"
    echo ""
    echo "📁 Structure:"
    echo "   app/             - Application code"
    echo "   logs/            - Log files"
    echo "   data/            - Workspace data"
    echo ""
    echo "🔧 Quick Commands:"
    echo "   $0 setup         - Initial setup"
    echo "   $0 start         - Start application"
    echo "   $0 test          - Run tests"
    echo "   $0 docker-start  - Start services"
    echo "   $0 logs          - View logs"
    echo "   $0 status        - Check status"
    echo ""
}

# Main menu
show_menu() {
    print_header "SimplerToday Development Menu"
    echo ""
    echo "1) Setup         - Initial project setup"
    echo "2) Start         - Start the application"
    echo "3) Test          - Run setup tests"
    echo "4) Docker Start  - Start Docker services"
    echo "5) Docker Stop   - Stop Docker services"
    echo "6) Docker Logs   - View Docker logs"
    echo "7) Logs          - View application logs"
    echo "8) Clean         - Clean temporary files"
    echo "9) Status        - Check project status"
    echo "0) Info          - Project information"
    echo "q) Quit"
    echo ""
}

# Main script
main() {
    if [[ $# -eq 0 ]]; then
        # Interactive mode
        while true; do
            show_menu
            read -p "Select option: " choice
            echo ""
            
            case $choice in
                1) cmd_setup ;;
                2) cmd_start ;;
                3) cmd_test ;;
                4) cmd_docker_start ;;
                5) cmd_docker_stop ;;
                6) cmd_docker_logs ;;
                7) cmd_logs ;;
                8) cmd_clean ;;
                9) cmd_status ;;
                0) cmd_info ;;
                q|Q) print_info "Goodbye!"; exit 0 ;;
                *) print_error "Invalid option" ;;
            esac
            
            echo ""
            read -p "Press Enter to continue..."
        done
    else
        # Command mode
        case $1 in
            setup) cmd_setup ;;
            start) cmd_start ;;
            test) cmd_test ;;
            docker-start) cmd_docker_start ;;
            docker-stop) cmd_docker_stop ;;
            docker-logs) cmd_docker_logs ;;
            logs) cmd_logs ;;
            clean) cmd_clean ;;
            status) cmd_status ;;
            info) cmd_info ;;
            *)
                print_error "Unknown command: $1"
                echo ""
                echo "Usage: $0 [command]"
                echo ""
                echo "Commands:"
                echo "  setup         - Initial project setup"
                echo "  start         - Start the application"
                echo "  test          - Run setup tests"
                echo "  docker-start  - Start Docker services"
                echo "  docker-stop   - Stop Docker services"
                echo "  docker-logs   - View Docker logs"
                echo "  logs          - View application logs"
                echo "  clean         - Clean temporary files"
                echo "  status        - Check project status"
                echo "  info          - Project information"
                echo ""
                echo "Or run without arguments for interactive menu"
                exit 1
                ;;
        esac
    fi
}

main "$@"

