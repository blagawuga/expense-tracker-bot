#!/bin/bash
# ─────────────────────────────────────────────────────────────
# Oracle Cloud Ubuntu Instance — Initial Setup Script
# Tuned for: VM.Standard.E2.1.Micro (1 OCPU, 1GB RAM, AMD)
# Run once after first SSH login:
#   chmod +x init-server.sh && sudo ./init-server.sh
# ─────────────────────────────────────────────────────────────

set -e  # exit on any error

# ── CONFIG ───────────────────────────────────────────────────
HOSTNAME="expense-bot"
TIMEZONE="Asia/Kolkata"
SWAP_SIZE="4G"              # 4x RAM — critical for 1GB instance
SSH_PORT=22
# ─────────────────────────────────────────────────────────────

# Must run as root
if [[ $EUID -ne 0 ]]; then
  echo "Run this script with sudo: sudo ./init-server.sh"
  exit 1
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  Oracle Cloud Instance Init Script"
echo "  Shape: VM.Standard.E2.1.Micro (1 OCPU, 1GB RAM)"
echo "═══════════════════════════════════════════════"
echo ""

# ── 1. SYSTEM UPDATE ─────────────────────────────────────────
echo "▶ [1/9] Updating system packages..."
apt update -y && apt upgrade -y && apt autoremove -y
echo "  ✓ Done"

# ── 2. TIMEZONE ──────────────────────────────────────────────
echo "▶ [2/9] Setting timezone to $TIMEZONE..."
timedatectl set-timezone "$TIMEZONE"
echo "  ✓ $(timedatectl | grep 'Time zone')"

# ── 3. HOSTNAME ──────────────────────────────────────────────
echo "▶ [3/9] Setting hostname to $HOSTNAME..."
hostnamectl set-hostname "$HOSTNAME"
grep -q "$HOSTNAME" /etc/hosts || echo "127.0.0.1 $HOSTNAME" >> /etc/hosts
echo "  ✓ Done"

# ── 4. UFW FIREWALL ──────────────────────────────────────────
echo "▶ [4/9] Configuring UFW firewall..."
apt install -y ufw
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow "$SSH_PORT"/tcp comment 'SSH'
ufw --force enable
echo "  ✓ UFW enabled — only port $SSH_PORT open inbound"

# ── 5. SSH HARDENING ─────────────────────────────────────────
echo "▶ [5/9] Hardening SSH config..."
SSHD_CONFIG="/etc/ssh/sshd_config"

set_ssh_option() {
  local key="$1"
  local value="$2"
  if grep -q "^$key" "$SSHD_CONFIG"; then
    sed -i "s/^$key.*/$key $value/" "$SSHD_CONFIG"
  elif grep -q "^#$key" "$SSHD_CONFIG"; then
    sed -i "s/^#$key.*/$key $value/" "$SSHD_CONFIG"
  else
    echo "$key $value" >> "$SSHD_CONFIG"
  fi
}

set_ssh_option "PermitRootLogin" "no"
set_ssh_option "PasswordAuthentication" "no"
set_ssh_option "X11Forwarding" "no"
set_ssh_option "MaxAuthTries" "3"

systemctl restart sshd
echo "  ✓ Root login disabled, password auth disabled"

# ── 6. FAIL2BAN ──────────────────────────────────────────────
echo "▶ [6/9] Installing and configuring Fail2Ban..."
apt install -y fail2ban

cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 3

[sshd]
enabled = true
port    = ssh
EOF

systemctl enable fail2ban
systemctl restart fail2ban
echo "  ✓ Fail2Ban active — bans after 3 failed SSH attempts"

# ── 7. SWAP ──────────────────────────────────────────────────
echo "▶ [7/9] Setting up ${SWAP_SIZE} swap space..."
if swapon --show | grep -q '/swapfile'; then
  echo "  ✓ Swap already exists, skipping"
else
  fallocate -l "$SWAP_SIZE" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "  ✓ ${SWAP_SIZE} swap created and mounted"
fi

# ── 8. MEMORY TUNING (critical for 1GB RAM) ──────────────────
echo "▶ [8/10] Tuning kernel memory settings..."

cat >> /etc/sysctl.conf << 'EOF'

# ── E2.1.Micro memory tuning ──────────────────────────────────
# Start using swap when RAM is 80% full (default is 40%)
vm.swappiness=20
# Reduce inode/dentry cache pressure to free memory faster
vm.vfs_cache_pressure=50
# Reduce dirty page writeback lag
vm.dirty_ratio=10
vm.dirty_background_ratio=5
EOF

sysctl -p > /dev/null
echo "  ✓ Kernel memory settings applied"

# ── 9. AUTO SECURITY UPDATES ─────────────────────────────────
echo "▶ [9/10] Enabling automatic security updates..."
apt install -y unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
echo "  ✓ Security updates will apply automatically"

# ── 10. ESSENTIAL PACKAGES ───────────────────────────────────
echo "▶ [10/10] Installing essential packages..."
# Minimal install — keep footprint small for 1GB RAM
apt install -y \
  python3 python3-pip python3-venv \
  git curl wget htop \
  ca-certificates
echo "  ✓ Done"

# ── SUMMARY ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════"
echo "  Setup Complete!"
echo "═══════════════════════════════════════════════"
echo ""
echo "  Shape     : VM.Standard.E2.1.Micro (1 OCPU, 1GB RAM)"
echo "  Hostname  : $(hostname)"
echo "  Timezone  : $(timedatectl | grep 'Time zone' | awk '{print $3}')"
echo "  RAM       : $(free -h | grep Mem | awk '{print $2}')"
echo "  Swap      : $(free -h | grep Swap | awk '{print $2}')"
echo "  Firewall  : $(ufw status | head -1)"
echo "  Fail2Ban  : $(systemctl is-active fail2ban)"
echo "  Python    : $(python3 --version)"
echo ""
echo "  Open ports:"
ufw status | grep ALLOW
echo ""
echo "  Memory overview:"
free -h
echo ""
echo "═══════════════════════════════════════════════"
echo ""
