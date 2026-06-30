#!/usr/bin/with-contenv bashio
set -euo pipefail

CGATE_DIR="/data/cgate"
SHARE_DIR="/share/cgate"
PACKAGE_DIR="/data/packages"
UPLOADED_PACKAGE="${PACKAGE_DIR}/cgate-package.zip"
PROJECT_STORE_DIR="/data/projects"
PROJECT_METADATA_FILE="${PROJECT_STORE_DIR}/metadata.json"
OPTIONS_FILE="/data/options.json"
INSTALL_MARKER="${CGATE_DIR}/.installed-package"

log_header() {
    bashio::log.info "C-Gate Server app v0.1.7"
}

verify_zip_safe() {
    local zip_path="$1"
    local bad_entries
    bad_entries="$(unzip -Z1 "${zip_path}" 2>/dev/null | awk '
        $0 ~ /(^|\/)\.\.(\/|$)/ { print; next }
        /^\// { print; next }
    ')"
    if [[ -n "${bad_entries}" ]]; then
        bashio::log.error "Archive contains unsafe paths:"
        bashio::log.error "${bad_entries}"
        return 1
    fi
}

set_config_key() {
    local file="$1"
    local key="$2"
    local value="$3"
    local key_re="${key//./\\.}"
    local tmp="${file}.tmp.$$"

    if grep -q "^${key_re}=" "${file}" 2>/dev/null; then
        sed "s|^${key_re}=.*|${key}=${value}|" "${file}" > "${tmp}"
        mv "${tmp}" "${file}"
    else
        printf '%s=%s\n' "${key}" "${value}" >> "${file}"
    fi
}

remove_config_key() {
    local file="$1"
    local key="$2"
    local key_re="${key//./\\.}"
    local tmp="${file}.tmp.$$"
    sed "/^${key_re}=/d" "${file}" > "${tmp}"
    mv "${tmp}" "${file}"
}

select_package() {
    local requested
    requested="$(jq -r '.package_filename // ""' "${OPTIONS_FILE}")"

    if [[ -f "${UPLOADED_PACKAGE}" ]]; then
        printf '%s' "${UPLOADED_PACKAGE}"
        return 0
    fi

    if [[ -d "${SHARE_DIR}" ]]; then
        if [[ -n "${requested}" && -f "${SHARE_DIR}/${requested}" ]]; then
            printf '%s' "${SHARE_DIR}/${requested}"
            return 0
        fi

        find "${SHARE_DIR}" -maxdepth 1 -type f -iname '*.zip' -print 2>/dev/null \
            | grep -Ei '/[^/]*c-?gate[^/]*\.zip$' \
            | sort \
            | tail -n 1
        return 0
    fi

    return 1
}

install_cgate() {
    local package_file="$1"
    local force_reinstall="$2"
    local package_signature
    local previous_signature=""

    package_signature="$(sha256sum "${package_file}" | awk '{print $1}')"
    if [[ -f "${INSTALL_MARKER}" ]]; then
        previous_signature="$(cat "${INSTALL_MARKER}" 2>/dev/null || true)"
    fi

    if [[ -f "${CGATE_DIR}/cgate.jar" && "${force_reinstall}" != "true" && "${package_signature}" == "${previous_signature}" ]]; then
        bashio::log.info "C-Gate is already installed from the selected package"
        return 0
    fi

    local work_dir
    work_dir="$(mktemp -d /tmp/cgate-install.XXXXXX)"
    trap 'rm -rf "${work_dir}"' RETURN

    bashio::log.info "Installing C-Gate from $(basename "${package_file}")"
    verify_zip_safe "${package_file}"
    unzip -q -o "${package_file}" -d "${work_dir}/extract"

    local jar_path
    jar_path="$(find "${work_dir}/extract" -type f -name 'cgate.jar' -print | head -n 1 || true)"

    if [[ -z "${jar_path}" ]]; then
        local nested_zip
        while IFS= read -r nested_zip; do
            [[ -z "${nested_zip}" ]] && continue
            bashio::log.info "Checking nested package $(basename "${nested_zip}")"
            verify_zip_safe "${nested_zip}"
            rm -rf "${work_dir}/nested"
            mkdir -p "${work_dir}/nested"
            unzip -q -o "${nested_zip}" -d "${work_dir}/nested"
            jar_path="$(find "${work_dir}/nested" -type f -name 'cgate.jar' -print | head -n 1 || true)"
            [[ -n "${jar_path}" ]] && break
        done < <(find "${work_dir}/extract" -type f -iname '*.zip' -print)
    fi

    if [[ -z "${jar_path}" ]]; then
        bashio::log.error "cgate.jar was not found in the supplied package"
        bashio::log.error "Use the official Schneider Electric Linux C-Gate package"
        return 1
    fi

    local source_dir
    source_dir="$(dirname "${jar_path}")"

    # Preserve project data and generated configuration during runtime upgrades.
    mkdir -p "${CGATE_DIR}"
    find "${CGATE_DIR}" -mindepth 1 -maxdepth 1 \
        ! -name 'Projects' \
        ! -name 'tag' \
        ! -name 'config' \
        ! -name 'logs' \
        ! -name '.installed-package' \
        -exec rm -rf -- {} +
    cp -a "${source_dir}/." "${CGATE_DIR}/"
    printf '%s' "${package_signature}" > "${INSTALL_MARKER}"
    chmod -R go-w "${CGATE_DIR}" 2>/dev/null || true

    bashio::log.info "C-Gate installed successfully"
}

sync_uploaded_projects() {
    mkdir -p "${PROJECT_STORE_DIR}" "${CGATE_DIR}/tag" "${PROJECT_STORE_DIR}/backups"

    if [[ ! -f "${PROJECT_METADATA_FILE}" ]]; then
        return 0
    fi

    local project_name
    project_name="$(jq -r '.active_project // ""' "${PROJECT_METADATA_FILE}")"
    if [[ -z "${project_name}" ]]; then
        return 0
    fi

    local stored_filename
    stored_filename="$(jq -r --arg project "${project_name}" '.projects[$project].stored_filename // ""' "${PROJECT_METADATA_FILE}")"

    # Migrate metadata created by v0.1.6 when possible.
    if [[ -z "${stored_filename}" ]]; then
        if [[ -f "${PROJECT_STORE_DIR}/${project_name}.db" ]]; then
            stored_filename="${project_name}.db"
        elif [[ -f "${PROJECT_STORE_DIR}/${project_name}.xml" ]]; then
            stored_filename="${project_name}.xml"
        fi
    fi

    if [[ -z "${stored_filename}" || ! -f "${PROJECT_STORE_DIR}/${stored_filename}" ]]; then
        bashio::log.warning "Uploaded project metadata exists, but the stored project file is missing"
        return 0
    fi

    local extension="${stored_filename##*.}"
    extension="${extension,,}"
    if [[ "${extension}" != "db" && "${extension}" != "xml" ]]; then
        bashio::log.error "Unsupported stored project format: ${stored_filename}"
        return 1
    fi

    local live_dir="${CGATE_DIR}/tag/${project_name}"
    local destination="${live_dir}/${project_name}.${extension}"
    local pending_apply
    pending_apply="$(jq -r --arg project "${project_name}" '.projects[$project].pending_apply // false' "${PROJECT_METADATA_FILE}")"

    if [[ "${pending_apply}" == "true" || ! -f "${destination}" ]]; then
        if [[ -d "${live_dir}" ]]; then
            local timestamp
            timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
            cp -a "${live_dir}" "${PROJECT_STORE_DIR}/backups/${project_name}-${timestamp}"
            bashio::log.info "Backed up existing C-Gate project ${project_name}"
        fi

        rm -rf "${live_dir}"
        mkdir -p "${live_dir}"
        cp -f "${PROJECT_STORE_DIR}/${stored_filename}" "${destination}"

        # Remove the incorrect legacy location used by app v0.1.6. Leaving it
        # behind can cause C-Gate to report a failed or pending XML conversion.
        rm -f "${CGATE_DIR}/Projects/${project_name}.xml"
        rm -rf "${CGATE_DIR}/Projects/${project_name}"

        local metadata_tmp="${PROJECT_METADATA_FILE}.tmp.$$"
        jq --arg project "${project_name}"             '.projects[$project].pending_apply = false'             "${PROJECT_METADATA_FILE}" > "${metadata_tmp}"
        mv "${metadata_tmp}" "${PROJECT_METADATA_FILE}"

        bashio::log.info "Installed project ${project_name} as tag/${project_name}/${project_name}.${extension}"
    else
        bashio::log.info "Using existing live C-Gate project ${project_name}"
    fi
}

resolve_project_name() {
    local configured
    configured="$(jq -r '.project_name // ""' "${OPTIONS_FILE}")"
    if [[ -n "${configured}" ]]; then
        printf '%s' "${configured}"
        return 0
    fi

    if [[ -f "${PROJECT_METADATA_FILE}" ]]; then
        jq -r '.active_project // ""' "${PROJECT_METADATA_FILE}" 2>/dev/null || true
    fi
}

write_access_file() {
    local access_file="${CGATE_DIR}/config/access.txt"
    mkdir -p "$(dirname "${access_file}")"

    cat > "${access_file}" <<'ACCESS'
## C-Gate Server Access Control File
## Generated by the Home Assistant C-Gate Server app.
interface 0:0:0:0:0:0:0:1 Program
interface 127.0.0.1 Program
interface localhost Program
remote 127.0.0.1 Program
ACCESS

    local client
    while IFS= read -r client; do
        [[ -z "${client}" ]] && continue
        if [[ "${client}" =~ [[:space:]] ]]; then
            bashio::log.warning "Ignoring invalid Toolkit client containing whitespace: ${client}"
            continue
        fi
        printf 'remote %s Program\n' "${client}" >> "${access_file}"
    done < <(jq -r '.toolkit_clients[]? // empty' "${OPTIONS_FILE}")

    while IFS= read -r client; do
        [[ -z "${client}" ]] && continue
        if [[ "${client}" =~ [[:space:]] ]]; then
            bashio::log.warning "Ignoring invalid integration client containing whitespace: ${client}"
            continue
        fi
        printf 'remote %s Program\n' "${client}" >> "${access_file}"
    done < <(jq -r '.integration_clients[]? // empty' "${OPTIONS_FILE}")

    sort -u "${access_file}" -o "${access_file}"
    bashio::log.info "Generated C-Gate access control file"
}

write_cgate_config() {
    local config_file="${CGATE_DIR}/config/C-GateConfig.txt"
    local project_name
    project_name="$(resolve_project_name)"

    mkdir -p "${CGATE_DIR}/config" "${CGATE_DIR}/tag" "${CGATE_DIR}/tag/archived" "${CGATE_DIR}/logs"
    touch "${config_file}"

    set_config_key "${config_file}" "project.default.dir" "tag/"
    set_config_key "${config_file}" "project.default.archive-dir" "tag/archived/"

    if [[ -n "${project_name}" ]]; then
        if [[ ! -f "${CGATE_DIR}/tag/${project_name}/${project_name}.db" && ! -f "${CGATE_DIR}/tag/${project_name}/${project_name}.xml" ]]; then
            bashio::log.warning "Default project ${project_name} is configured but no C-Gate project database exists in tag/${project_name}/"
        fi
        set_config_key "${config_file}" "project.default" "${project_name}"
        set_config_key "${config_file}" "project.start" "${project_name}"
        bashio::log.info "Configured C-Gate to start project: ${project_name}"
    else
        remove_config_key "${config_file}" "project.default"
        remove_config_key "${config_file}" "project.start"
        bashio::log.info "No default project configured. Upload a Toolkit project through Open Web UI."
    fi
}

main() {
    log_header

    if [[ ! -f "${OPTIONS_FILE}" ]]; then
        bashio::log.error "Options file not found: ${OPTIONS_FILE}"
        exit 1
    fi

    mkdir -p "${CGATE_DIR}" "${PACKAGE_DIR}" "${PROJECT_STORE_DIR}"

    python3 /upload_server.py &
    local upload_pid=$!
    bashio::log.info "Runtime and Toolkit project upload UI is available through Open Web UI"

    local package_file=""
    while [[ -z "${package_file}" || ! -f "${package_file}" ]]; do
        package_file="$(select_package || true)"
        if [[ -z "${package_file}" || ! -f "${package_file}" ]]; then
            bashio::log.warning "No C-Gate package found. Open the app Web UI and upload the official Schneider Electric Linux package."
            sleep 10
        fi
        if ! kill -0 "${upload_pid}" 2>/dev/null; then
            bashio::log.error "Upload UI stopped unexpectedly"
            exit 1
        fi
    done

    local force_reinstall
    force_reinstall="$(jq -r '.force_reinstall // false' "${OPTIONS_FILE}")"
    install_cgate "${package_file}" "${force_reinstall}"
    sync_uploaded_projects
    write_access_file
    write_cgate_config

    local java_max_memory
    java_max_memory="$(jq -r '.java_max_memory_mb // 512' "${OPTIONS_FILE}")"

    bashio::log.info "Starting Schneider Electric C-Gate"
    bashio::log.info "Command port: 20023"
    bashio::log.info "Event port: 20024"
    bashio::log.info "Load-change port: 20025"
    bashio::log.info "Configuration-change port: 20026"
    bashio::log.info "Secure command port: 20123"
    bashio::log.info "Secure event port: 20124"
    bashio::log.info "Secure status-change port: 20125"
    bashio::log.info "Secure configuration-change port: 20126"

    cd "${CGATE_DIR}"
    exec java \
        -Djava.awt.headless=true \
        -Xms64m \
        -Xmx"${java_max_memory}"m \
        --add-opens=java.xml/com.sun.org.apache.xml.internal.serialize=ALL-UNNAMED \
        -jar "${CGATE_DIR}/cgate.jar" \
        -s
}

main "$@"
