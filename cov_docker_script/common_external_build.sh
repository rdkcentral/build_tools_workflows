#!/usr/bin/env bash
set -e

################################################################################
# Common External Build Script
# Orchestrates the complete build process: dependencies + native component
# Usage: ./common_external_build.sh [config_file] [component_dir]
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${1:-$SCRIPT_DIR/component_config.json}"
COMPONENT_DIR="${2:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# Source common utilities
source "$SCRIPT_DIR/common_build_utils.sh"

# Validate inputs
if [[ ! -f "$CONFIG_FILE" ]]; then
    err "Config file not found: $CONFIG_FILE"
    exit 1
fi

if [[ ! -d "$COMPONENT_DIR" ]]; then
    err "Component directory not found: $COMPONENT_DIR"
    exit 1
fi

# Get component name from config
COMPONENT_NAME=$(jq -r '.native_component.name' "$CONFIG_FILE")

# Print main banner
echo ""
echo -e "${BOLD}${BLUE}================================================================${NC}"
echo -e "${BOLD}${BLUE}    Complete Build Pipeline for: ${COMPONENT_NAME}${NC}"
echo -e "${BOLD}${BLUE}================================================================${NC}"
echo ""
log "Configuration: $CONFIG_FILE"
log "Component directory: $COMPONENT_DIR"
echo ""

# Step 1: Setup Dependencies
print_banner "Step 1/2: Setting Up Dependencies"
log "Running dependency setup script..."
echo ""

# Propagate build optimization environment to nested builds
export BUILD_DIR="${BUILD_DIR:-$HOME/build}"
export USR_DIR="${USR_DIR:-$HOME/usr}"
export FORCE_REBUILD="${FORCE_REBUILD:-false}"
export BUILD_SKIP_DEBUG="${BUILD_SKIP_DEBUG:-false}"

# Mark that we're in a nested build to help with debugging
export BUILD_RECURSION_LEVEL=$((${BUILD_RECURSION_LEVEL:-0} + 1))

if [[ "${BUILD_SKIP_DEBUG:-false}" == "true" && ${BUILD_RECURSION_LEVEL:-0} -gt 1 ]]; then
    warn "Nested build detected (recursion level: $BUILD_RECURSION_LEVEL)"
fi

if ! "$SCRIPT_DIR/setup_dependencies.sh" "$CONFIG_FILE"; then
    err "Dependency setup failed"
    exit 1
fi

echo ""
ok "Dependencies setup completed successfully"
echo ""

# Step 2: Build Native Component
print_banner "Step 2/2: Building Native Component"
log "Running native component build script..."
echo ""

if ! "$SCRIPT_DIR/build_native.sh" "$CONFIG_FILE" "$COMPONENT_DIR"; then
    err "Native component build failed"
    exit 1
fi

echo ""
ok "Native component build completed successfully"
echo ""

# Final summary
echo ""
echo -e "${BOLD}${GREEN}================================================================${NC}"
echo -e "${BOLD}${GREEN}    Complete Build Pipeline Completed Successfully!${NC}"
echo -e "${BOLD}${GREEN}================================================================${NC}"
echo ""
log "Component: ${BOLD}$COMPONENT_NAME${NC}"
log "All dependencies built and installed"
log "Native component compiled successfully"
echo ""

# Display installation paths
HEADER_PATH=$(jq -r '.native_component.include_path' "$CONFIG_FILE")
LIB_PATH=$(jq -r '.native_component.lib_output_path' "$CONFIG_FILE")
HEADER_PATH="${HEADER_PATH//\$HOME/$HOME}"
LIB_PATH="${LIB_PATH//\$HOME/$HOME}"

echo -e "${CYAN}Installation Locations:${NC}"
log "  Headers: $HEADER_PATH"
log "  Libraries: $LIB_PATH"
echo ""

echo -e "${GREEN}✓ Ready for Coverity analysis or deployment${NC}"
echo ""
