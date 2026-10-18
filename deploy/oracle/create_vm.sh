#!/bin/bash
# Run inside Oracle Cloud Shell. Creates an Always-Free VM with a RESERVED (static)
# public IP, opens ports 22/80/443, and boots it with vm_setup.sh (Docker + app + HTTPS).
set -euo pipefail
T="${OCI_TENANCY:?Run this in Oracle Cloud Shell}"
NAME=jackpot
RAW=https://raw.githubusercontent.com/prag1987/Jackpot/live-trading/deploy/oracle
cd ~
curl -fsSL "$RAW/vm_setup.sh" -o jackpot_vm_setup.sh
[ -f ~/.ssh/id_rsa ] || ssh-keygen -t rsa -b 4096 -N '' -f ~/.ssh/id_rsa -q

say(){ echo -e "\n==> $*"; }
AD=$(oci iam availability-domain list -c "$T" --query 'data[0].name' --raw-output)

say "Network"
VCN=$(oci network vcn list -c "$T" --display-name ${NAME}-vcn --query 'data[0].id' --raw-output 2>/dev/null || true)
if [ -z "$VCN" ] || [ "$VCN" = "null" ]; then
  VCN=$(oci network vcn create -c "$T" --cidr-blocks '["10.0.0.0/16"]' --display-name ${NAME}-vcn \
        --dns-label $NAME --wait-for-state AVAILABLE --query data.id --raw-output)
  IGW=$(oci network internet-gateway create -c "$T" --vcn-id "$VCN" --is-enabled true \
        --display-name ${NAME}-igw --wait-for-state AVAILABLE --query data.id --raw-output)
  RT=$(oci network vcn get --vcn-id "$VCN" --query 'data."default-route-table-id"' --raw-output)
  oci network route-table update --rt-id "$RT" --force \
    --route-rules "[{\"cidrBlock\":\"0.0.0.0/0\",\"networkEntityId\":\"$IGW\"}]" >/dev/null
  SL=$(oci network vcn get --vcn-id "$VCN" --query 'data."default-security-list-id"' --raw-output)
  ING='['
  for p in 22 80 443; do ING+="{\"source\":\"0.0.0.0/0\",\"protocol\":\"6\",\"tcpOptions\":{\"destinationPortRange\":{\"min\":$p,\"max\":$p}}},"; done
  ING="${ING%,}]"
  oci network security-list update --security-list-id "$SL" --force --ingress-security-rules "$ING" \
    --egress-security-rules '[{"destination":"0.0.0.0/0","protocol":"all"}]' >/dev/null
  oci network subnet create -c "$T" --vcn-id "$VCN" --cidr-block 10.0.0.0/24 --display-name ${NAME}-subnet \
    --dns-label pub --wait-for-state AVAILABLE >/dev/null
fi
SUB=$(oci network subnet list -c "$T" --vcn-id "$VCN" --query 'data[0].id' --raw-output)

say "VM (tries Ampere A1 2 CPU/12 GB, then AMD micro 1 GB)"
INST=$(oci compute instance list -c "$T" --display-name ${NAME}-vps --lifecycle-state RUNNING --query 'data[0].id' --raw-output 2>/dev/null || true)
launch(){ # shape shape-config
  local img
  img=$(oci compute image list -c "$T" --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
        --shape "$1" --sort-by TIMECREATED --sort-order DESC --query 'data[0].id' --raw-output)
  oci compute instance launch -c "$T" --availability-domain "$AD" --shape "$1" ${2:+--shape-config "$2"} \
    --image-id "$img" --subnet-id "$SUB" --assign-public-ip false --display-name ${NAME}-vps \
    --ssh-authorized-keys-file ~/.ssh/id_rsa.pub --user-data-file ~/jackpot_vm_setup.sh \
    --wait-for-state RUNNING --max-wait-seconds 900 --query data.id --raw-output
}
if [ -z "$INST" ] || [ "$INST" = "null" ]; then
  INST=$(launch VM.Standard.A1.Flex '{"ocpus":2,"memoryInGBs":12}' 2>/tmp/a1.err) || {
    echo "A1 not available ($(grep -o '"message": "[^"]*' /tmp/a1.err | head -1)). Trying micro..."
    INST=$(launch VM.Standard.E2.1.Micro "")
  }
fi

say "Reserved (static) public IP"
VNIC=$(oci compute instance list-vnics --instance-id "$INST" --query 'data[0].id' --raw-output)
PRIV=$(oci network private-ip list --vnic-id "$VNIC" --query 'data[0].id' --raw-output)
IP=$(oci network public-ip get --private-ip-id "$PRIV" --query 'data."ip-address"' --raw-output 2>/dev/null || true)
if [ -z "$IP" ] || [ "$IP" = "null" ]; then
  IP=$(oci network public-ip create -c "$T" --lifetime RESERVED --private-ip-id "$PRIV" \
       --display-name ${NAME}-static-ip --query 'data."ip-address"' --raw-output)
fi
echo "$IP" > ~/jackpot_ip.txt
HOST="$(echo "$IP" | tr . -).sslip.io"
cat <<EOF

=====================================================
 Static IP (Dhan whitelist):  $IP
 Dashboard URL (~10 min):     https://$HOST
 Check progress:  ssh ubuntu@$IP 'tail -5 /var/log/jackpot-setup.log'
 Dashboard password:  ssh ubuntu@$IP 'sudo cat /root/dashboard_password.txt'
=====================================================
EOF
