#!/usr/bin/env bash

################################################################################
# Common Build Utilities
# Shared functions for dependency and component builds
################################################################################

# Colors
RED="\e[31m"; GREEN="\e[32m"; YELLOW="\e[33m"
BLUE="\e[34m"; CYAN="\e[36m"; BOLD="\e[1m"; NC="\e[0m"

# Logging functions
log()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; }
step() { echo -e "${BLUE}[STEP]${NC} $1"; }

# Expand $HOME in paths
expand_path() {
    echo "${1//\$HOME/$HOME}"
}

# Validate required tools
check_dependencies() {
    local required_tools=("git" "jq" "gcc" "make")
    local missing=()
    
    for tool in "${required_tools[@]}"; do
        if ! command -v "$tool" &> /dev/null; then
            missing+=("$tool")
        fi
    done
    
    if [ ${#missing[@]} -gt 0 ]; then
        err "Missing required tools: ${missing[*]}"
        err "Please install them before continuing"
        return 1
    fi
    return 0
}

# Clone a git repository
clone_repo() {
    local name="$1" repo="$2" branch="$3" dest="$4"
    
    if [[ -d "$dest" ]]; then
        warn "$name already exists, skipping clone"
        return 0
    fi
    
    log "Cloning $name (branch: $branch)"
    if ! git clone --branch "$branch" "$repo" "$dest" --depth 1; then
        err "Failed to clone $name"
        return 1
    fi
    ok "$name cloned successfully"

    if [[ -f "$dest/.gitmodules" ]]; then
        git -C "$dest" submodule update --init --recursive --remote || {
            err "$name git submodule update failed"
            return 1
        }
        ok "$name git submodule update done!"
    else
        ok "$name has no submodules, skipping submodule update"
    fi
    return 0
}

# Copy headers from source to destination
copy_headers() {
    local src="$1" dst="$2"
    
    src=$(expand_path "$src")
    dst=$(expand_path "$dst")
    
    mkdir -p "$dst"
    
    if [[ -d "$src" ]]; then
        log "Copying headers: $src → $dst"
        if ! find "$src" -maxdepth 1 -name "*.h*" -exec cp {} "$dst/" \; 2>/dev/null; then
            warn "No headers found in $src"
        fi
    else
        warn "Header source not found: $src"
    fi
}

# Generic API to copy all Python files from a source directory (recursively) to a destination directory (flat, no subdirs)
copy_python_files_generic() {
    local src_dir="${PYTHON_SRC_DIR:-$HOME/build}"
    local dst_dir="${PYTHON_DST_DIR:-$HOME/usr/include/rdkb}"
    if [[ -n "$src_dir" && -n "$dst_dir" ]]; then
        log "[PYTHON COPY] Scanning for Python files in: $src_dir"
        mkdir -p "$dst_dir"
        local py_files
        py_files=$(find "$src_dir" -type f -name "*.py")
        local count=0
        if [[ -n "$py_files" ]]; then
            log "[PYTHON COPY] Copying Python files to: $dst_dir"
            while IFS= read -r file; do
                cp "$file" "$dst_dir/"
                count=$((count+1))
            done <<< "$py_files"
            ok "[PYTHON COPY] $count Python file(s) copied to $dst_dir"
        else
            warn "[PYTHON COPY] No Python files found in $src_dir"
        fi
    else
        warn "[PYTHON COPY] Source or destination directory not set. Skipping copy."
    fi
}

# Apply source patches
apply_patch() {
    local file="$1" search="$2" replace="$3" type="${4:-replace}" content="$5"
    
    if [[ "$type" == "create" ]]; then
        log "Creating file: $file"
        local dir=$(dirname "$file")
        mkdir -p "$dir"
        echo -e "$content" > "$file"
        if [[ $? -ne 0 ]]; then
            err "Failed to create file: $file"
            return 1
        fi
        ok "File created successfully"
        return 0
    fi
    
    if [[ ! -f "$file" ]]; then
        err "Patch target not found: $file"
        return 1
    fi
    
    log "Patching: $file ($type)"
    
    # Use python for safe string replacement with literal matching
    if ! python3 -c "
import sys
with open('$file', 'r') as f:
    content = f.read()
content = content.replace('''$search''', '''$replace''')
with open('$file', 'w') as f:
    f.write(content)
"; then
        err "Failed to apply patch to $file"
        return 1
    fi
    
    ok "Patch applied successfully"
    return 0
}

# Build with autotools
build_autotools() {
    local repo_dir="$1" configure_flags="$2" make_targets="$3" parallel_make="${4:-true}"
    
    pushd "$repo_dir" >/dev/null || return 1
    
    # Run autogen or autoreconf if needed
    if [[ -f "autogen.sh" ]]; then
        step "Running autogen.sh"
        chmod +x autogen.sh
        # Set NOCONFIGURE to prevent autogen.sh from automatically running configure
        if ! NOCONFIGURE=1 ./autogen.sh; then
            err "autogen.sh failed"
            popd >/dev/null
            return 1
        fi
    elif [[ -f "configure.ac" ]] || [[ -f "configure.in" ]]; then
        step "Running autoreconf"
        if ! autoreconf -fi; then
            err "autoreconf failed"
            popd >/dev/null
            return 1
        fi
    fi
    
    # Configure
    step "Running configure"
    # Ensure PKG_CONFIG_PATH is set for configure
    export PKG_CONFIG_PATH="${HOME}/usr/local/lib/pkgconfig:${HOME}/usr/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
    if ! eval "./configure $configure_flags"; then
        err "Configure failed"
        popd >/dev/null
        return 1
    fi
    
    # Make
    local make_jobs=""
    [[ "$parallel_make" == "true" ]] && make_jobs="-j$(nproc)"
    
    step "Running make $make_jobs $make_targets"
    if ! make $make_jobs $make_targets; then
        err "Make failed"
        popd >/dev/null
        return 1
    fi
    
    popd >/dev/null
    ok "Autotools build completed"
    return 0
}

# Build with CMake
build_cmake() {
    local repo_dir="$1" build_dir="$2" cmake_flags="$3" make_targets="$4" parallel_make="${5:-true}"
    
    pushd "$repo_dir" >/dev/null || return 1
    mkdir -p "$build_dir"
    
    step "Running cmake"
    if ! eval "cmake -S . -B $build_dir $cmake_flags"; then
        err "CMake configuration failed"
        popd >/dev/null
        return 1
    fi
    
    local make_jobs=""
    [[ "$parallel_make" == "true" ]] && make_jobs="-j$(nproc)"
    
    step "Building with make $make_jobs $make_targets"
    if ! make $make_jobs -C "$build_dir" $make_targets; then
        err "Make failed"
        popd >/dev/null
        return 1
    fi
    
    popd >/dev/null
    ok "CMake build completed"
    return 0
}

# Build with Meson
build_meson() {
    local repo_dir="$1" build_dir="$2" meson_flags="$3" ninja_targets="$4"
    
    pushd "$repo_dir" >/dev/null || return 1
    
    step "Running meson setup"
    if ! eval "meson setup $build_dir $meson_flags"; then
        err "Meson setup failed"
        popd >/dev/null
        return 1
    fi
    
    step "Running ninja -C $build_dir $ninja_targets"
    if ! ninja -C "$build_dir" $ninja_targets; then
        err "Ninja build failed"
        popd >/dev/null
        return 1
    fi
    
    popd >/dev/null
    ok "Meson build completed"
    return 0
}

# Execute custom commands
execute_commands() {
    local repo_dir="$1" config_file="$2" index="$3"
    
    # Read command count and commands BEFORE changing directories
    local cmd_count
    cmd_count=$(jq ".dependencies.repos[$index].build.commands | length" "$config_file")
    
    # Read all commands into an array before pushd
    local commands=()
    local i=0
    while [[ $i -lt $cmd_count ]]; do
        commands+=("$(jq -r ".dependencies.repos[$index].build.commands[$i]" "$config_file")")
        i=$((i + 1))
    done

    pushd "$repo_dir" >/dev/null || return 1

    # Execute commands from the array
    i=0
    while [[ $i -lt $cmd_count ]]; do
        local cmd="${commands[$i]}"
        step "Executing: $cmd"
        if ! eval "$cmd"; then
            err "Command failed: $cmd"
            popd >/dev/null
            return 1
        fi
        i=$((i + 1))
    done
    
    popd >/dev/null
    ok "Commands executed successfully"
    return 0
}

# Get expected library names from build configuration files
get_expected_libraries() {
    local repo_dir="$1"
    local expected_libs=()
    
    # Check for Makefile.am files (autotools)
    if find "$repo_dir" -name "Makefile.am" -type f 2>/dev/null | grep -q .; then
        while IFS= read -r makefile; do
            # Extract lib_LTLIBRARIES entries (libtool libraries)
            # Format: lib_LTLIBRARIES = libname.la
            local ltlibs=$(grep -E '^\s*lib_LTLIBRARIES\s*[+=]' "$makefile" 2>/dev/null | \
                sed 's/.*=\s*//g' | tr ' ' '\n' | grep -E '\.la$' | sed 's/\.la$//')
            
            # Extract noinst_LTLIBRARIES (non-installed libraries, still built)
            local noinst_ltlibs=$(grep -E '^\s*noinst_LTLIBRARIES\s*[+=]' "$makefile" 2>/dev/null | \
                sed 's/.*=\s*//g' | tr ' ' '\n' | grep -E '\.la$' | sed 's/\.la$//')
            
            # Combine both
            for lib in $ltlibs $noinst_ltlibs; do
                [[ -n "$lib" ]] && expected_libs+=("$lib")
            done
        done < <(find "$repo_dir" -name "Makefile.am" -type f 2>/dev/null)
    fi
    
    # Check for CMakeLists.txt files (cmake)
    if find "$repo_dir" -name "CMakeLists.txt" -type f 2>/dev/null | grep -q .; then
        while IFS= read -r cmake_file; do
            # Extract add_library entries
            # Format: add_library(name SHARED/STATIC ...)
            local cmake_libs=$(grep -E 'add_library\s*\(' "$cmake_file" 2>/dev/null | \
                sed -E 's/.*add_library\s*\(\s*([a-zA-Z0-9_-]+).*/\1/' | grep -v '^add_library')
            
            for lib in $cmake_libs; do
                [[ -n "$lib" && "$lib" != "add_library" ]] && expected_libs+=("lib$lib")
            done
        done < <(find "$repo_dir" -name "CMakeLists.txt" -type f 2>/dev/null)
    fi
    
    # Check for meson.build files
    if find "$repo_dir" -name "meson.build" -type f 2>/dev/null | grep -q .; then
        while IFS= read -r meson_file; do
            # Extract shared_library and static_library entries
            # Format: shared_library('name', ...)
            local meson_libs=$(grep -E "(shared|static)_library\s*\(" "$meson_file" 2>/dev/null | \
                sed -E "s/.*(shared|static)_library\s*\(\s*'([^']+)'.*/\2/" | grep -v 'library')
            
            for lib in $meson_libs; do
                [[ -n "$lib" && "$lib" != "library" ]] && expected_libs+=("lib$lib")
            done
        done < <(find "$repo_dir" -name "meson.build" -type f 2>/dev/null)
    fi
    
    # Return unique library names
    printf '%s\n' "${expected_libs[@]}" | sort -u
}

# Copy shared libraries to destination
copy_libraries() {
    local src_dir="$1" dst_dir="$2"
    
    dst_dir=$(expand_path "$dst_dir")
    mkdir -p "$dst_dir"
    
    log "Copying libraries to $dst_dir"
    find "$src_dir" \( -name "*.so*" -o -name "*.a" -o -name "*.la*" \) \( -type f -o -type l \) -exec cp -Pv {} "$dst_dir/" \; 2>/dev/null || true
}

# Check if a component is already built (with intelligent library detection)
is_component_already_built() {
    local component_name="$1"
    local build_dir="$2"
    local lib_path="$3"
    
    # Expand paths
    build_dir=$(expand_path "$build_dir")
    lib_path=$(expand_path "$lib_path")
    
    local component_dir="$build_dir/$component_name"
    
    # Check if build directory exists
    if [[ ! -d "$component_dir" ]]; then
        [[ "${BUILD_SKIP_DEBUG:-false}" == "true" ]] && warn "Build dir missing: $component_dir"
        return 1
    fi
    
    # Check if lib_path exists
    if [[ ! -d "$lib_path" ]]; then
        [[ "${BUILD_SKIP_DEBUG:-false}" == "true" ]] && warn "Lib path missing: $lib_path"
        return 1
    fi
    
    # Get expected libraries from build files (Makefile.am, CMakeLists.txt, meson.build)
    local expected_libs
    expected_libs=$(get_expected_libraries "$component_dir")
    
    # If no libraries are expected, this is a header-only dependency
    if [[ -z "$expected_libs" ]]; then
        log "$component_name appears to be header-only (no libraries in build files)"
        return 0  # Skip rebuild for header-only components
    fi
    
    # Check if ALL expected libraries exist
    local all_found=true
    local missing_libs=()
    local found_libs=()
    
    while IFS= read -r expected_lib; do
        [[ -z "$expected_lib" ]] && continue
        
        # Remove 'lib' prefix if present for searching
        local lib_base="${expected_lib#lib}"
        
        # Search for the library file (.so, .so.*, .a, .la)
        local lib_file=$(find "$lib_path" -maxdepth 1 \( \
            -name "${expected_lib}.so*" -o \
            -name "${expected_lib}.a" -o \
            -name "${expected_lib}.la" -o \
            -name "lib${lib_base}.so*" -o \
            -name "lib${lib_base}.a" -o \
            -name "lib${lib_base}.la" \
        \) -type f 2>/dev/null | head -1)
        
        if [[ -n "$lib_file" ]]; then
            found_libs+=("$lib_file")
        else
            missing_libs+=("$expected_lib")
            all_found=false
        fi
    done <<< "$expected_libs"
    
    # Debug output
    if [[ "${BUILD_SKIP_DEBUG:-false}" == "true" ]]; then
        local expected_count=$(echo "$expected_libs" | grep -c '^' 2>/dev/null || echo 0)
        log "$component_name expects $expected_count library/libraries:"
        echo "$expected_libs" | while IFS= read -r lib; do
            [[ -n "$lib" ]] && log "  - Expected: $lib"
        done
        
        if [[ ${#found_libs[@]} -gt 0 ]]; then
            log "Found ${#found_libs[@]} library/libraries:"
            for lib in "${found_libs[@]}"; do
                log "  ✓ $(basename "$lib")"
            done
        fi
        
        if [[ ${#missing_libs[@]} -gt 0 ]]; then
            warn "Missing ${#missing_libs[@]} library/libraries:"
            for lib in "${missing_libs[@]}"; do
                warn "  ✗ $lib"
            done
        fi
    fi
    
    # Return based on whether all libraries were found
    if [[ "$all_found" == "true" ]]; then
        return 0  # All expected libraries found
    else
        return 1  # Some libraries missing
    fi
}

# Print banner
print_banner() {
    local title="$1"
    echo ""
    echo -e "${BLUE}================================================${NC}"
    echo -e "${BLUE}    $title${NC}"
    echo -e "${BLUE}================================================${NC}"
    echo ""
}

# Print section header
print_section() {
    local title="$1"
    echo ""
    echo -e "${BLUE}------------------------------------------------${NC}"
    echo -e "${BOLD}${CYAN}▶ $title${NC}"
    echo -e "${BLUE}------------------------------------------------${NC}"
}

# Export this file's functions
export -f log ok warn err step
export -f expand_path check_dependencies clone_repo copy_headers apply_patch
export -f build_autotools build_cmake build_meson execute_commands copy_libraries
export -f get_expected_libraries is_component_already_built
export -f print_banner print_section
