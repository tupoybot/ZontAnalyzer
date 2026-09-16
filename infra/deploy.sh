#!/bin/sh
# Plans, logs and credentials remain in a private directory outside tracked files.
set -eu
umask 077
[ "$#" = 2 ] || { echo 'Usage: infra/deploy.sh plan|apply|plan-foundation|apply-foundation PRIVATE_DIRECTORY' >&2; exit 2; }
ACTION=$1
PRIVATE=$(realpath "$2")
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TOOLS=${M1_TOOLS_IMAGE:-zont-m1-tools:local}
case "$ACTION" in plan|apply|plan-foundation|apply-foundation) ;; *) exit 2 ;; esac
[ -d "$PRIVATE/cloud-work" ]
[ -f "$PRIVATE/cloud-work/inputs.tfvars.json" ]
[ -f "$PRIVATE/cloud.tfbackend.json" ]
[ -f "$PRIVATE/state.env" ]
[ -f "$PRIVATE/deploy-token" ]

docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$ROOT/infra/scripts,dst=/scripts,readonly" \
  --mount "type=bind,src=$PRIVATE,dst=/private,readonly" \
  "$TOOLS" python /scripts/check-scope.py /private/scope.json /private/cloud-work/inputs.tfvars.json /private/cloud.tfbackend.json

if [ "$ACTION" = plan ] || [ "$ACTION" = plan-foundation ]; then
  # The private work directory mirrors this root; remove retired source files.
  for file in "$PRIVATE/cloud-work/"*.tf; do
    [ -f "$file" ] || continue
    [ -f "$ROOT/infra/cloud/$(basename "$file")" ] || rm -- "$file"
  done
  cp "$ROOT"/infra/cloud/*.tf "$ROOT/infra/cloud/.terraform.lock.hcl" "$PRIVATE/cloud-work/"
fi

docker run --rm --user "$(id -u):$(id -g)" \
  --env-file "$PRIVATE/state.env" -e "M1_ACTION=$ACTION" \
  --mount "type=bind,src=$ROOT/infra/scripts,dst=/scripts,readonly" \
  --mount "type=bind,src=$PRIVATE,dst=/private" \
  --workdir /private/cloud-work "$TOOLS" sh -c '
    set -eu
    umask 077
    export YC_TOKEN="$(cat /private/deploy-token)"
    if [ "$M1_ACTION" = plan ] || [ "$M1_ACTION" = plan-foundation ]; then
      terraform init -input=false -no-color -lockfile=readonly \
        -backend-config=/private/cloud.tfbackend.json > /private/deploy-init.log 2>&1
      set --
      if [ "$M1_ACTION" = plan-foundation ]; then
        set -- -target=yandex_container_registry.project -target=yandex_lockbox_secret.probe
      fi
      terraform plan "$@" -input=false -no-color -lock-timeout=30s \
        -var-file=inputs.tfvars.json -out=deployment.tfplan > /private/deploy-plan.log 2>&1
      terraform show -json deployment.tfplan > /private/deploy-plan.json
    else
      terraform show -json deployment.tfplan > /private/deploy-plan.json
      python /scripts/check-scope.py /private/scope.json /private/deploy-plan.json /private/cloud.tfbackend.json
      if [ "$M1_ACTION" = apply-foundation ]; then
        jq -e '\''[.resource_changes[]? | select(.mode == "managed" and .change.actions != ["no-op"])]
          | all(.change.actions == ["create"] and
            (.address == "yandex_container_registry.project" or .address == "yandex_lockbox_secret.probe"))'\'' \
          /private/deploy-plan.json > /dev/null
      fi
      terraform apply -input=false -no-color -lock-timeout=30s \
        deployment.tfplan > /private/deploy-apply.log 2>&1
      terraform output -json > /private/cloud-outputs.json
      if [ "$M1_ACTION" = apply-foundation ]; then
        exit 0
      fi
      python /scripts/bound_revision.py /private > /private/scaling.log 2>&1
      terraform plan -refresh-only -input=false -no-color -lock-timeout=30s \
        -var-file=inputs.tfvars.json -out=scaling-refresh.tfplan > /private/scaling-refresh.log 2>&1
      terraform apply -input=false -no-color -lock-timeout=30s \
        scaling-refresh.tfplan >> /private/scaling-refresh.log 2>&1
      terraform output -json > /private/cloud-outputs.json
      python /scripts/runtime_smoke.py /private > /private/runtime-smoke.log 2>&1
    fi
  '
printf 'Terraform %s completed; evidence is in the private directory.\n' "$ACTION"
