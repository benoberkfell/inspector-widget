# shellcheck shell=bash
#
# Inspector Widget — pick a JDK that can run this project's Gradle build.
#
# Sourced by scripts/build.sh and scripts/install-a11yprobe.sh. Defines
# select_jdk, which exports JAVA_HOME (and prepends $JAVA_HOME/bin to PATH), or
# prints why not and returns 1. Written for bash 3.2 (macOS /bin/bash).
#
# Supported range: JDK 17-23. AGP 8.7.2 needs >= 17; the Gradle 8.13 wrapper
# cannot run on JDK 24+. The modules compile to Java 17 bytecode, so any JDK in
# range produces the same artifacts.
#
# Order of preference:
#   1. an explicit JAVA_HOME, if it is a JDK in range (else warn and keep looking)
#   2. macOS: /usr/libexec/java_home -F -v N  (LTS 21, 17 first, then 23..18)
#   3. the `java` on PATH (its real java.home, not the /usr/bin shim)
#   4. Linux: /usr/lib/jvm/*
# Every candidate is verified with its own `java -version`: `java_home -v N`
# without -F exits 0 and returns some other JDK when N is not installed.

JDK_MIN=17
JDK_MAX=23

# Print the major version of the JDK at $1 (e.g. 21), or return 1.
jdk_major() {
    local home="$1" v
    [ -n "$home" ] && [ -x "$home/bin/java" ] || return 1
    # First line with `version "..."`; ignores "Picked up JAVA_TOOL_OPTIONS" noise.
    v="$("$home/bin/java" -version 2>&1 | awk -F'"' '/ version "/ { print $2; exit }')"
    case "$v" in 1.*) v="${v#1.}" ;; esac   # 1.8.0_x -> 8
    v="${v%%[.+-]*}"                        # 21.0.11 / 23-ea / 17+35 -> major
    case "$v" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "$v"
}

# True if $1 is a full JDK (has javac) whose major is within [JDK_MIN, JDK_MAX].
jdk_in_range() {
    local major
    [ -x "$1/bin/javac" ] || return 1
    major="$(jdk_major "$1")" || return 1
    [ "$major" -ge "$JDK_MIN" ] && [ "$major" -le "$JDK_MAX" ]
}

# The real java.home of the `java` on PATH (resolves /usr/bin and alternatives shims).
_path_java_home() {
    command -v java >/dev/null 2>&1 || return 1
    java -XshowSettings:properties -version 2>&1 \
        | awk -F' = ' '/^[[:space:]]*java\.home = / { print $2; exit }'
}

# Newline-separated candidate JAVA_HOMEs, most preferred first.
_jdk_candidates() {
    local v home
    if [ -x /usr/libexec/java_home ]; then
        for v in 21 17 23 22 20 19 18; do
            home="$(/usr/libexec/java_home -F -v "$v" 2>/dev/null)" && printf '%s\n' "$home"
        done
    fi
    home="$(_path_java_home)" && [ -n "$home" ] && printf '%s\n' "$home"
    for home in /usr/lib/jvm/*; do
        [ -d "$home" ] && printf '%s\n' "$home"
    done
    return 0
}

select_jdk() {
    local cand major
    if [ -n "${JAVA_HOME:-}" ]; then
        if jdk_in_range "$JAVA_HOME"; then
            export JAVA_HOME
            export PATH="$JAVA_HOME/bin:$PATH"
            return 0
        fi
        if major="$(jdk_major "$JAVA_HOME")" && [ -x "$JAVA_HOME/bin/javac" ]; then
            major="JDK $major"
        else
            major="not a JDK (no bin/java + bin/javac)"
        fi
        printf '\033[1;33m[warn]\033[0m JAVA_HOME=%s is %s; this build needs JDK %s-%s. Looking for another JDK...\n' \
            "$JAVA_HOME" "$major" "$JDK_MIN" "$JDK_MAX" >&2
    fi
    while IFS= read -r cand; do
        [ -n "$cand" ] || continue
        if jdk_in_range "$cand"; then
            JAVA_HOME="$cand"
            export JAVA_HOME
            export PATH="$JAVA_HOME/bin:$PATH"
            return 0
        fi
    done <<EOF
$(_jdk_candidates)
EOF
    printf '\033[1;31m[fail]\033[0m No JDK %s-%s found. AGP 8.7.2 needs JDK >= 17 and the Gradle 8.13 wrapper cannot run on JDK 24+.\n' \
        "$JDK_MIN" "$JDK_MAX" >&2
    printf '       Install one (e.g. Temurin or Corretto 21) or export JAVA_HOME=/path/to/jdk-21.\n' >&2
    return 1
}
