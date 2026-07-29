#!/bin/bash
set -e

REGION="us-east-1"
INSTANCE_TYPE="t3.medium"
DISK_SIZE=8
KEY_NAME="vm-ocp-ci-deploy"
SSH_KEY="$HOME/.ssh/id_ed25519"
VPC_CIDR="10.99.0.0/24"
VPC_NAME="ocp-ci-bastion-vpc"
SG_NAME="ocp-ci-bastion-sg"
IMAGE="quay.io/vbnrh/ocp-dr-pipeline:latest"
BASTION_FILE="$(dirname "$0")/../config/bastion.yaml"
AWS="aws --region $REGION"
VERSION=""

usage() {
  echo "Usage: $0 [--version <ocp-version|latest>] [--cleanup]"
  echo "  --version   OCP nightly version (default: bundled jul-16)"
  echo "  --cleanup   Terminate EC2 and delete bastion VPC"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --version) VERSION="$2"; shift 2 ;;
    --cleanup) CLEANUP=true; shift ;;
    *) usage ;;
  esac
done

# --- Cleanup mode ---
if [ "$CLEANUP" = true ]; then
  echo "Cleaning up bastion resources..."
  if [ -f "$BASTION_FILE" ]; then
    INST=$(grep instance_id "$BASTION_FILE" | awk '{print $2}')
    VPC=$(grep vpc_id "$BASTION_FILE" | awk '{print $2}')
    [ -n "$INST" ] && $AWS ec2 terminate-instances --instance-ids "$INST" 2>/dev/null && echo "Terminated $INST"
    sleep 30
    if [ -n "$VPC" ]; then
      for subnet in $($AWS ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" --query 'Subnets[*].SubnetId' --output text 2>/dev/null); do
        $AWS ec2 delete-subnet --subnet-id "$subnet" 2>/dev/null
      done
      for igw in $($AWS ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$VPC" --query 'InternetGateways[*].InternetGatewayId' --output text 2>/dev/null); do
        $AWS ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$VPC" 2>/dev/null
        $AWS ec2 delete-internet-gateway --internet-gateway-id "$igw" 2>/dev/null
      done
      for sg in $($AWS ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC" --query "SecurityGroups[?GroupName!='default'].GroupId" --output text 2>/dev/null); do
        $AWS ec2 delete-security-group --group-id "$sg" 2>/dev/null
      done
      $AWS ec2 delete-vpc --vpc-id "$VPC" 2>/dev/null && echo "Deleted VPC $VPC"
    fi
    rm -f "$BASTION_FILE"
  fi
  echo "Cleanup complete."
  exit 0
fi

# --- Prerequisites ---
echo "Checking prerequisites..."
for f in "$SSH_KEY" "$SSH_KEY.pub" "data/pull-secret" "data/auth.yaml" "$HOME/.aws/credentials"; do
  [ -f "$f" ] || { echo "Missing: $f"; exit 1; }
done
echo "All prerequisites found."

# --- VPC setup (idempotent) ---
VPC_ID=$($AWS ec2 describe-vpcs --filters "Name=tag:Name,Values=$VPC_NAME" --query 'Vpcs[0].VpcId' --output text 2>/dev/null)
if [ "$VPC_ID" = "None" ] || [ -z "$VPC_ID" ]; then
  echo "Creating bastion VPC..."
  VPC_ID=$($AWS ec2 create-vpc --cidr-block "$VPC_CIDR" --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=$VPC_NAME}]" --query 'Vpc.VpcId' --output text)
  $AWS ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support
  $AWS ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames
  SUBNET_ID=$($AWS ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$VPC_CIDR" --availability-zone "${REGION}a" --query 'Subnet.SubnetId' --output text)
  IGW_ID=$($AWS ec2 create-internet-gateway --query 'InternetGateway.InternetGatewayId' --output text)
  $AWS ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
  RT_ID=$($AWS ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" --query 'RouteTables[0].RouteTableId' --output text)
  $AWS ec2 create-route --route-table-id "$RT_ID" --destination-cidr-block "0.0.0.0/0" --gateway-id "$IGW_ID" >/dev/null
  SG_ID=$($AWS ec2 create-security-group --group-name "$SG_NAME" --description "SSH for bastion" --vpc-id "$VPC_ID" --query 'GroupId' --output text)
  $AWS ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 --cidr "0.0.0.0/0" >/dev/null
  echo "Created VPC $VPC_ID"
else
  SUBNET_ID=$($AWS ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" --query 'Subnets[0].SubnetId' --output text)
  SG_ID=$($AWS ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$SG_NAME" --query 'SecurityGroups[0].GroupId' --output text)
  echo "Reusing VPC $VPC_ID"
fi

# --- Key pair (idempotent) ---
$AWS ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1 || \
  $AWS ec2 import-key-pair --key-name "$KEY_NAME" --public-key-material "fileb://$SSH_KEY.pub" >/dev/null

# --- Launch EC2 ---
echo "Launching EC2 instance..."
AMI=$($AWS ec2 describe-images --owners amazon --filters "Name=name,Values=al2023-ami-2023.*-x86_64" "Name=state,Values=available" --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text)
INSTANCE_ID=$($AWS ec2 run-instances \
  --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" --security-group-ids "$SG_ID" \
  --subnet-id "$SUBNET_ID" --associate-public-ip-address \
  --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":$DISK_SIZE,\"VolumeType\":\"gp3\"}}]" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=ocp-dr-pipeline}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "Instance: $INSTANCE_ID"

$AWS ec2 wait instance-running --instance-ids "$INSTANCE_ID"
EC2_IP=$($AWS ec2 describe-instances --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "Public IP: $EC2_IP"

# --- Wait for SSH ---
echo "Waiting for SSH..."
for i in $(seq 1 30); do
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$SSH_KEY" ec2-user@"$EC2_IP" "echo ready" 2>/dev/null && break
  sleep 10
done

# --- SCP secrets ---
echo "Copying secrets..."
ssh -i "$SSH_KEY" ec2-user@"$EC2_IP" "mkdir -p ~/secrets ~/.aws ~/.ssh"
scp -i "$SSH_KEY" data/pull-secret "ec2-user@$EC2_IP:~/secrets/pull-secret"
scp -i "$SSH_KEY" data/auth.yaml "ec2-user@$EC2_IP:~/secrets/auth.yaml"
scp -i "$SSH_KEY" "$HOME/.aws/credentials" "ec2-user@$EC2_IP:~/.aws/credentials"
scp -i "$SSH_KEY" "$SSH_KEY" "ec2-user@$EC2_IP:~/.ssh/id_ed25519"
scp -i "$SSH_KEY" "$SSH_KEY.pub" "ec2-user@$EC2_IP:~/.ssh/id_ed25519.pub"
ssh -i "$SSH_KEY" ec2-user@"$EC2_IP" "chmod 600 ~/.ssh/id_ed25519"

# --- Pull and run container ---
echo "Starting pipeline container..."
ssh -i "$SSH_KEY" ec2-user@"$EC2_IP" bash <<REMOTE
sudo podman pull $IMAGE
sudo podman run -d --name pipeline \
  -v /home/ec2-user/secrets/pull-secret:/app/data/pull-secret:ro \
  -v /home/ec2-user/secrets/auth.yaml:/app/data/auth.yaml:ro \
  -v /home/ec2-user/.aws:/root/.aws:ro \
  -v /home/ec2-user/.ssh:/root/.ssh:ro \
  -v /tmp:/tmp \
  -e AWS_PROFILE=poweruser \
  -e OPENSHIFT_INSTALL_EXPERIMENTAL_DISABLE_IMAGE_POLICY=true \
  -e OCP_VERSION="${VERSION}" \
  $IMAGE
echo "Pipeline container started"
REMOTE

# --- Save bastion details ---
cat > "$BASTION_FILE" <<EOF
instance_id: $INSTANCE_ID
public_ip: $EC2_IP
region: $REGION
instance_type: $INSTANCE_TYPE
vpc_id: $VPC_ID
subnet_id: $SUBNET_ID
security_group_id: $SG_ID
image: $IMAGE
ocp_version: ${VERSION:-bundled}
ssh_command: ssh -i $SSH_KEY ec2-user@$EC2_IP
monitor: ssh -i $SSH_KEY ec2-user@$EC2_IP sudo podman logs -f pipeline
EOF

echo ""
echo "========================================="
echo "Pipeline running on EC2"
echo "========================================="
echo "SSH:     ssh -i $SSH_KEY ec2-user@$EC2_IP"
echo "Monitor: ssh -i $SSH_KEY ec2-user@$EC2_IP sudo podman logs -f pipeline"
echo "Cleanup: $0 --cleanup"
echo "========================================="
