#!/bin/sh
# Launch one benchmark machine. It powers itself off after MAX_HOURS.
# Usage: infra/launch.sh <aws-profile> [alert-email]
#   INSTANCE_TYPE, VOLUME_GB, MAX_HOURS, NAME and AMI override the defaults. The
#   image is UBUNTU_AMI from versions.env unless AMI says otherwise.
# Writes infra/instance.env, or ENV_FILE (not committed), for sync.sh.
set -eu
cd "$(dirname "$0")/.."
TYPE_OVERRIDE=${INSTANCE_TYPE:-}
. ./versions.env
INSTANCE_TYPE=${TYPE_OVERRIDE:-$INSTANCE_TYPE}
PROFILE=$1 EMAIL=${2:-}
NAME=${NAME:-omar-rerun} VOLUME_GB=${VOLUME_GB:-100} MAX_HOURS=${MAX_HOURS:-200}
aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }

AMI=${AMI:-$UBUNTU_AMI}
KEY=$HOME/.ssh/$NAME-$PROFILE.pem
if [ ! -f "$KEY" ]; then
    aws ec2 create-key-pair --key-name "$NAME" --key-type ed25519 \
        --query KeyMaterial --output text > "$KEY"
    chmod 600 "$KEY"
fi

VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" "Name=vpc-id,Values=$VPC" \
    --query 'SecurityGroups[0].GroupId' --output text)
if [ "$SG" = None ]; then
    SG=$(aws ec2 create-security-group --group-name "$NAME" --description "ssh to $NAME" \
        --vpc-id "$VPC" --query GroupId --output text)
fi
MYIP=$(curl -fsS https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 \
    --cidr "$MYIP/32" >/dev/null 2>&1 || true   # already allowed

# Power off after MAX_HOURS even if nobody is watching; a stopped instance
# keeps its disk and stops billing for compute.
USERDATA=$(printf '#!/bin/sh\nshutdown -h +%s\n' "$((MAX_HOURS * 60))")
ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
    --key-name "$NAME" --security-group-ids "$SG" \
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_GB,VolumeType=gp3,DeleteOnTermination=true}" \
    --metadata-options HttpTokens=required \
    --instance-initiated-shutdown-behavior stop --user-data "$USERDATA" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
    --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$ID"
HOST=$(aws ec2 describe-instances --instance-ids "$ID" \
    --query 'Reservations[0].Instances[0].PublicDnsName' --output text)

# Alert on gross spend: with credits on the account, net cost stays at $0 and
# an alert that counted credits would never fire.
if [ -n "$EMAIL" ]; then
    ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
    command aws --profile "$PROFILE" --region us-east-1 budgets create-budget --account-id "$ACCOUNT" \
        --budget '{"BudgetName":"'"$NAME"'","BudgetLimit":{"Amount":"90","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST","CostTypes":{"IncludeCredit":false,"IncludeRefund":false}}' \
        --notifications-with-subscribers '[{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80,"ThresholdType":"PERCENTAGE"},"Subscribers":[{"SubscriptionType":"EMAIL","Address":"'"$EMAIL"'"}]}]' \
        2>/dev/null || echo "budget $NAME already exists"
fi

printf 'PROFILE=%s\nINSTANCE_ID=%s\nHOST=%s\nKEY=%s\nAMI=%s\nTYPE=%s\n' \
    "$PROFILE" "$ID" "$HOST" "$KEY" "$AMI" "$INSTANCE_TYPE" > "${ENV_FILE:-infra/instance.env}"
echo "$ID $INSTANCE_TYPE $AMI $HOST"
