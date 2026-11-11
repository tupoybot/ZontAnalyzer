#!/bin/sh
# Retry a Terraform plan only when a provider reports a transient gRPC outage.
set -eu

[ "$#" -ge 3 ] || {
    echo 'Usage: terraform_plan_retry.sh LOG MAX_ATTEMPTS COMMAND [ARG...]' >&2
    exit 2
}

log=$1
max_attempts=$2
shift 2
case "$max_attempts" in '' | *[!0-9]* | 0) exit 2 ;; esac

attempt=1
: > "$log"
while :; do
    attempt_log="${log}.attempt-${attempt}"
    if "$@" > "$attempt_log" 2>&1; then
        cat "$attempt_log" >> "$log"
        rm -f "$attempt_log"
        exit 0
    else
        status=$?
    fi

    printf '\nTerraform plan attempt %s of %s failed.\n' "$attempt" "$max_attempts" >> "$log"
    cat "$attempt_log" >> "$log"
    rm -f "$attempt_log"
    if [ "$attempt" -ge "$max_attempts" ] || ! tail -n 200 "$log" | grep -Eq '(^|[^[:alpha:]])Unavailable([^[:alpha:]]|$)'; then
        exit "$status"
    fi
    attempt=$((attempt + 1))
    sleep "${TERRAFORM_PLAN_RETRY_DELAY_SECONDS:-10}"
done
