# 🔧 Switch-Config

A Python automation tool that **zero-touch provisions a factory-default Cisco IOS switch** via a serial console cable — completely replacing the need for a manual PuTTY session. It automatically bypasses the IOS setup wizard, discovers the switch's DHCP IP address, and pushes a full configuration over Telnet.

---

## 📌 The Problem It Solves

When a brand-new or factory-reset Cisco switch powers on, it launches an interactive setup wizard and has no IP address. The traditional workflow requires an engineer to:

1. Open PuTTY → connect to the COM port manually
2. Sit through the wizard, answering prompts one by one
3. Manually configure an IP address
4. Open a second PuTTY/SSH session to push the full config

**This tool replaces all of that with a single command:** `python provisioner.py`

---

## 🗂️ Project Structure

```
├── provisioner.py      # Main automation script
├── config.py           # All settings — edit this before running
└── README.md
```

---

## ⚙️ Configuration (`config.py`)

All parameters are in one place. **Edit `config.py` before running.**

```
# ─── Provisional switch (already configured) ──────────────────────────
PROVISIONAL_IP   = "192.168.1.1"
PROVISIONAL_USER = "admin"
PROVISIONAL_PASS = "admin"

# ─── Target switch — what you WANT it to become ───────────────────────
TARGET_HOSTNAME  = "name your hostname"
TARGET_NEW_USER  = "Name your user"
TARGET_NEW_PASS  = "Give your password"
TARGET_ENABLE    = "Give your enable password"

# ─── Default credentials on the unconfigured target switch ────────────
TARGET_DEFAULT_USER = ""
TARGET_DEFAULT_PASS = "Password"

# ─── Console cable settings ───────────────────────────────────────────
CONSOLE_PORT     = "COM4"    # ← change to your actual COM port
CONSOLE_BAUDRATE = 9600      # standard Cisco console baud rate
```

| Parameter | Description |
|---|---|
| `PROVISIONAL_IP/USER/PASS` | A pre-configured switch (reserved for future use, not used in current flow) |
| `TARGET_HOSTNAME` | Hostname to assign to the new switch |
| `TARGET_NEW_USER` | Local username to create with privilege 15 |
| `TARGET_NEW_PASS` | Password for the new local user |
| `TARGET_ENABLE` | Enable secret password |
| `TARGET_DEFAULT_USER` | Leave blank for factory-default switches |
| `TARGET_DEFAULT_PASS` | Temporary password set on vty lines during Phase 0 |
| `CONSOLE_PORT` | Windows COM port (check Device Manager) |
| `CONSOLE_BAUDRATE` | 9600 for all standard Cisco switches |

---

## 🚀 How It Works — Phase by Phase

### Phase 0 — Console: Bypass Wizard, Enable Telnet, Discover IP

This is the core of the tool. It opens the serial console port using `pyserial` and fully automates the switch boot process.

**Step-by-step:**

**1. Opens the COM port** at 9600 baud (8N1 — standard Cisco console settings) and waits up to **5 minutes** for the switch to boot.

**2. Automatically answers all setup wizard prompts** using real-time regex pattern matching on the serial buffer:

| Prompt Detected | Response Sent |
|---|---|
| `Would you like to enter the initial configuration dialog` | `no` |
| `Would you like to terminate autoinstall` | `no` |
| `Would you like to enter basic management setup` | `no` |
| `Would you like to go through AutoInstall` | `no` |
| Any `password:` or `secret:` prompt | Value from `TARGET_DEFAULT_PASS` |
| `Confirm enable secret:` | Value from `TARGET_DEFAULT_PASS` |
| `Configure SNMP Network Management` | `no` |
| `[yes/no]` | `no` |
| `[y/n]` | `n` |
| `--More--` (pagination) | Space |
| `Press RETURN to get started` | Enter |
| Any `hostname#` or `hostname>` prompt | Stop — CLI is ready |

If nothing arrives for 10 seconds, it sends a nudge (`\r\n`) to keep the switch responsive.

**3. Enters enable mode** on the CLI prompt.

**4. Enables Telnet on vty lines 0–15** with a temporary password (from `TARGET_DEFAULT_PASS`). This opens the door for Phase 2.

**5. Brings up Vlan1 with DHCP:**
```
interface vlan1
 no shutdown
 ip address dhcp
```
Then waits **20 seconds** for the DHCP lease to be assigned.

**6. Reads the switch IP from the console** by sending `show ip interface brief` and parsing the output with regex to find the Vlan1 IP. Retries every 10 seconds for up to **2 minutes**.

Returns the discovered IP address to the main function.

---

### Phase 1 — Skipped

Originally reserved for reading the IP via a provisional switch. **This phase is skipped** — the IP is read directly from the console in Phase 0, making this step unnecessary.

---

### Phase 2 — Push Full Configuration via Telnet

Uses **Netmiko** (`cisco_ios_telnet` device type) to connect to the discovered IP and push the final configuration.

**Connects with:**
- Host: IP discovered in Phase 0
- Username: `TARGET_DEFAULT_USER` (blank for factory switches)
- Password: `TARGET_DEFAULT_PASS`
- Enable secret: value from config

**Configuration pushed via `build_config()`:**

| Config Item | Detail |
|---|---|
| Hostname | Set to `TARGET_HOSTNAME` |
| Local user | `TARGET_NEW_USER` at privilege 15 with secret `TARGET_NEW_PASS` |
| Enable secret | Set to `TARGET_ENABLE` |
| MAC address tracking | `mac address-table notification change` and `mac-move` enabled |

**After config push:**

- Generates a **2048-bit RSA key** for SSH (`crypto key generate rsa modulus 2048`) — waits ~30 seconds
- Forces legacy SSH algorithms via Paramiko (for older Cisco IOS compatibility):
  - KEX: `diffie-hellman-group14-sha1`, `group-exchange-sha1`, `group1-sha1`
  - MACs: `hmac-sha1`, `hmac-sha1-96`
  - Keys: `ssh-rsa`
  - Ciphers: AES-128/192/256-CBC/CTR, 3DES-CBC
- Re-enters enable mode (RSA generation can drop the session)
- Verifies the hostname by reading the CLI prompt
- Disconnects cleanly

---

## 📋 Prerequisites

### Hardware
- Windows PC with a free COM port (or USB-to-serial adapter)
- Cisco RJ45-to-DB9 console cable
- Factory-default (or erased) Cisco IOS switch
- DHCP server reachable on the switch's management network

### Python 3.8+

Install all dependencies:

```bash
pip install pyserial netmiko paramiko rich
```

| Library | Version | Purpose |
|---|---|---|
| `pyserial` | any | Serial console communication (Phase 0) |
| `netmiko` | any | Telnet connection and config push (Phase 2) |
| `paramiko` | any | SSH transport with legacy Cisco algorithm support |
| `rich` | any | Colored, formatted terminal output |

---

## 🖥️ Usage

### Before You Run

1. **Connect** the console cable from your PC to the switch's console port.
2. **Find your COM port** → Windows Device Manager → Ports (COM & LPT). Update `CONSOLE_PORT` in `config.py`.
3. **Close PuTTY** (or any other app using the COM port) — `pyserial` needs exclusive access.
4. **Edit `config.py`** — set `TARGET_HOSTNAME`, `TARGET_NEW_USER`, `TARGET_NEW_PASS`, `TARGET_ENABLE`.
5. **Power on the switch** (or issue `reload` + erase NVRAM to start fresh).

### Run

```bash
python provisioner.py
```

### Expected Output

```
╭──────────────────────────────────────────╮
│ Cisco Single-Switch Provisioner          │
│ Target hostname : ACCESS-SW-01           │
│ Provisional SW  : 192.168.1.1            │
│ Console port    : COM4                   │
╰──────────────────────────────────────────╯

Phase 0 — Bypassing setup wizard via console
  Opening console port COM4 at 9600 baud...
  ✓ Console port open
  Waiting for switch to boot and show setup wizard...
  ✓ Switch reached CLI prompt
  Entering enable mode...
  Enabling Telnet on vty lines...
  ✓ Telnet enabled
  Bringing Vlan1 up and requesting DHCP lease...
  ✓ Vlan1 brought up — waiting 20 s for DHCP lease...
  Reading IP address from switch via console...
  ✓ Target switch IP: 10.*.*.*

Phase 1 — Skipped (IP read directly from console: 10.*.*.*)

Phase 2 — Pushing config to target switch (10.*.*.*)
  ✓ Connected via Telnet
  ✓ Entered enable mode
  Pushing hostname & credentials config...
  Generating RSA key (this takes ~30 s)...
  Re-entering enable mode before save...
  ✓ Hostname confirmed: ACCESS-SW-01

╭──────────────────────────────────────────╮
│ ✓ All done!                              │
│ Switch is now named ACCESS-SW-01         │
│ SSH in with: ssh YourUser@10.*.*.*       │
╰──────────────────────────────────────────╯
```

---

## 🛠️ Troubleshooting

| Problem | Likely Cause | Fix |
|---|---|---|
| `Could not open COM4` | Port in use or wrong port | Close PuTTY; check Device Manager for the correct COM number |
| `Could not reach CLI prompt within 5 minutes` | Wrong baud rate or cable issue | Try `CONSOLE_BAUDRATE = 115200` in `config.py`; check cable and adapter |
| `Authentication failed` on Telnet | Password mismatch | Ensure `TARGET_DEFAULT_PASS` matches the password set on vty lines in Phase 0 |
| `Telnet connection failed` | No DHCP lease or wrong network | Check DHCP server; verify Vlan1 is connected to a network with a DHCP scope |
| Hostname not confirmed after push | RSA key generation timed out | SSH in manually and run `show run` to verify; re-run if needed |
| Script hangs after boot | Another app was using the COM port during Phase 0 | Restart the switch and rerun after fully closing PuTTY |

---

## 🔒 Security Notes

> ⚠️ **Never commit `config.py` with real credentials to a public repository.**

- Add `config.py` to `.gitignore` and provide a `config.example.py` with placeholder values for teammates.
- The temporary Telnet password (set in Phase 0) is only active until Phase 2 completes and SSH is configured.
- For production environments, consider rotating the `TARGET_DEFAULT_PASS` after provisioning.

---

## 📄 License

Internal use only.
