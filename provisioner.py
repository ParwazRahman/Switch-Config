import time
import re
import sys

import serial
import paramiko
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
from rich.console import Console
from rich.panel import Panel

console = Console()

from config import (
    PROVISIONAL_IP, PROVISIONAL_USER, PROVISIONAL_PASS,
    TARGET_HOSTNAME, TARGET_NEW_USER, TARGET_NEW_PASS, TARGET_ENABLE,
    TARGET_DEFAULT_USER, TARGET_DEFAULT_PASS,
    CONSOLE_PORT, CONSOLE_BAUDRATE,
)


def _set_legacy_algorithms() -> None:
    """Force Paramiko to accept old Cisco IOS SSH algorithms globally."""
    paramiko.Transport._preferred_kex = (
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group-exchange-sha1",
        "diffie-hellman-group1-sha1",
    )
    paramiko.Transport._preferred_macs = (
        "hmac-sha1",
        "hmac-sha1-96",
    )
    paramiko.Transport._preferred_keys = (
        "ssh-rsa",
    )
    paramiko.Transport._preferred_ciphers = (
        "aes128-cbc",
        "aes192-cbc",
        "aes256-cbc",
        "3des-cbc",
        "aes128-ctr",
        "aes192-ctr",
        "aes256-ctr",
    )


def _send_and_read(ser: serial.Serial, cmd: bytes, wait: float = 1.0) -> str:
    """Send a command over serial and return whatever comes back."""
    ser.write(cmd)
    time.sleep(wait)
    raw = ser.read(ser.in_waiting or 1)
    output = raw.decode(errors="ignore") if raw else ""
    if output:
        console.print(output, style="dim", end="")
    return output


# ──────────────────────────────────────────────────────────────────────────
# PHASE 0 — Console: bypass wizard, enable Vlan1 + Telnet, read IP
# ──────────────────────────────────────────────────────────────────────────

def bypass_setup_wizard() -> str:
    """
    1. Open console port
    2. Answer every setup wizard prompt automatically
    3. Enable Telnet on vty lines
    4. Bring Vlan1 up with DHCP
    5. Read and return the IP address
    """
    console.print("\n[bold cyan]Phase 0 — Bypassing setup wizard via console[/bold cyan]")
    console.print(f"  Opening console port [bold]{CONSOLE_PORT}[/bold] at {CONSOLE_BAUDRATE} baud...")

    try:
        ser = serial.Serial(
            port=CONSOLE_PORT,
            baudrate=CONSOLE_BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1,
        )
    except serial.SerialException as e:
        console.print(f"  [red]✗ Could not open {CONSOLE_PORT}: {e}[/red]")
        console.print("  Check the COM port in config.py and that no other program (PuTTY etc.) is using it.")
        sys.exit(1)

    console.print(f"  [green]✓ Console port open[/green]")
    console.print("  Waiting for switch to boot and show setup wizard...")

    # Wake up any existing prompt
    ser.write(b"\r\n")
    time.sleep(2)

    boot_complete  = False
    deadline       = time.time() + 300    # 5 min max for boot
    buffer         = ""
    last_send_time = 0

    PROMPT_RESPONSES = [
        # ── Goal: any hostname prompt → stop ──────────────────────────
        (r"[A-Za-z0-9_\-]+[>\#]\s*$",                               None),

        # ── Wizard entry questions ─────────────────────────────────────
        (r"Would you like to enter the initial configuration dialog", "no\r\n"),
        (r"Would you like to terminate autoinstall",                  "no\r\n"),
        (r"Would you like to enter basic management setup",           "no\r\n"),
        (r"Would you like to go through AutoInstall",                 "no\r\n"),

        # ── All password/secret prompts ────────────────────────────────
        (r"(?i)(secret|password)\s*:\s*$",                           "Password\r\n"),
        (r"Confirm enable secret:",                 "Password\r\n"),
        # ── Other wizard questions ─────────────────────────────────────
        (r"Configure SNMP Network Management",                        "no\r\n"),
        (r"Enter interface name used to connect",                     "\r\n"),
        (r"Configuring interface",                                    "\r\n"),
        (r"Press RETURN to get started",                              "\r\n"),
        (r"\[yes/no\]",                                               "no\r\n"),
        (r"\[y/n\]",                                                  "n\r\n"),
        (r"--More--",                                                 " "),
    ]

    while time.time() < deadline:
        raw = ser.read(ser.in_waiting or 1)

        if raw:
            text = raw.decode(errors="ignore")
            buffer += text
            console.print(text, end="", style="dim")

            if len(buffer) > 2000:
                buffer = buffer[-2000:]

            for pattern, response in PROMPT_RESPONSES:
                if re.search(pattern, buffer, re.IGNORECASE):
                    if response is None:
                        console.print("\n  [green]✓ Switch reached CLI prompt[/green]")
                        boot_complete = True
                        break
                    now = time.time()
                    if now - last_send_time > 1.0:
                        time.sleep(0.5)
                        ser.write(response.encode())
                        last_send_time = now
                        buffer = ""
                    break

            if boot_complete:
                break
        else:
            # Nudge with Enter every 10 s if nothing coming
            if time.time() - last_send_time > 10:
                ser.write(b"\r\n")
                last_send_time = time.time()
            time.sleep(0.5)

    if not boot_complete:
        console.print(
            "\n  [red]✗ Could not reach CLI prompt within 5 minutes.[/red]\n"
            "  • Check cables and COM port\n"
            "  • Try baud rate 115200 in config.py\n"
            "  • Make sure no other program is using the COM port"
        )
        ser.close()
        sys.exit(1)

    # ── Step A: Enter enable mode ──────────────────────────────────────
    console.print("\n  Entering enable mode...")
    time.sleep(1)
    _send_and_read(ser, b"enable\r\n",  wait=1)
    _send_and_read(ser, b"Password\r\n",  wait=1)   # blank enable password

    # ── Step B: Enable Telnet on vty lines ────────────────────────────
    console.print("  Enabling Telnet on vty lines...")
    telnet_commands = [
        b"conf t\r\n",
        b"line vty 0 15\r\n",
        b" transport input telnet\r\n",
        b" login\r\n",
        b" password Password\r\n",
        b"exit\r\n",
        b"exit\r\n",
    ]
    for cmd in telnet_commands:
        _send_and_read(ser, cmd, wait=1)

    console.print("  [green]✓ Telnet enabled (password: cisco)[/green]")

    # ── Step C: Bring Vlan1 up with DHCP ──────────────────────────────
    console.print("  Bringing Vlan1 up and requesting DHCP lease...")
    vlan1_commands = [
        b"conf t\r\n",
        b"interface vlan1\r\n",
        b" no shutdown\r\n",
        b" ip address dhcp\r\n",
        b"exit\r\n",
        b"exit\r\n",
    ]
    for cmd in vlan1_commands:
        _send_and_read(ser, cmd, wait=1)

    console.print("  [green]✓ Vlan1 brought up — waiting 20 s for DHCP lease...[/green]")
    time.sleep(20)

    # ── Step D: Read IP from switch via console ────────────────────────
    console.print("  Reading IP address from switch via console...")

    target_ip = None
    deadline2 = time.time() + 120    # 2 min max

    while time.time() < deadline2 and not target_ip:

        # Clear stale buffer
        ser.reset_input_buffer()
        time.sleep(1)

        # Make sure we are in enable mode
        _send_and_read(ser, b"\r\n",       wait=0.5)
        _send_and_read(ser, b"enable\r\n", wait=0.5)
        _send_and_read(ser, b"Password\r\n", wait=0.5)   # blank enable password

        # Send show command
        ser.reset_input_buffer()
        ser.write(b"show ip interface brief\r\n")

        # Read until prompt returns (full output received)
        output        = ""
        read_deadline = time.time() + 15
        while time.time() < read_deadline:
            time.sleep(0.5)
            raw = ser.read(ser.in_waiting or 1)
            if raw:
                chunk = raw.decode(errors="ignore")
                output += chunk
            if re.search(r"[A-Za-z0-9_\-]+[>\#]\s*$", output):
                break

        # Print raw output so we can debug if needed
        console.print(f"\n  [dim]--- output ---\n{repr(output)}\n  --- end ---[/dim]")

        # Match any valid IP on Vlan1
        match = re.search(
            r"Vlan1\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+YES",
            output
        )

        if match:
            ip = match.group(1)
            if not ip.startswith("0.") and ip != "unassigned":
                target_ip = ip
            else:
                console.print("  [dim]Vlan1 has no IP yet, retrying in 10 s...[/dim]")
                time.sleep(10)
        else:
            if "Vlan1" in output:
                console.print("  [dim]Vlan1 up but no IP yet, retrying in 10 s...[/dim]")
            else:
                console.print("  [dim]Vlan1 not in output yet, retrying in 10 s...[/dim]")
            time.sleep(10)

    ser.close()

    if not target_ip:
        console.print(
            "\n  [red]✗ Could not read IP from switch within 2 minutes.[/red]\n"
            "  Run 'show ip interface brief' manually on the console to check."
        )
        sys.exit(1)

    console.print(f"\n  [green]✓ Target switch IP: [bold]{target_ip}[/bold][/green]")
    return target_ip


# ──────────────────────────────────────────────────────────────────────────
# PHASE 2 — Build config commands
# ──────────────────────────────────────────────────────────────────────────

def build_config() -> list:
    """Return IOS config commands for hostname + credentials."""
    return [
        f"hostname {TARGET_HOSTNAME}",
        f"username {TARGET_NEW_USER} privilege 15 secret {TARGET_NEW_PASS}",
        f"enable secret {TARGET_ENABLE}",
        # ──────────────────────────────────────────────────────────────────────────
        # Your hardening commands
        # ──────────────────────────────────────────────────────────────────────────
        "mac address-table notification change",
        "mac address-table notification mac-move",
    ]


# ──────────────────────────────────────────────────────────────────────────
# PHASE 2 — Push config to target switch via Telnet
# ──────────────────────────────────────────────────────────────────────────

def push_config(target_ip: str) -> None:
    """Connect to target switch via Telnet and push config."""
    console.print(f"\n[bold cyan]Phase 2 — Pushing config to target switch ({target_ip})[/bold cyan]")

    _set_legacy_algorithms()

    device = {
        "device_type"        : "cisco_ios_telnet",
        "host"               : target_ip,
        "username"           : TARGET_DEFAULT_USER,
        "password"           : TARGET_DEFAULT_PASS,
        "secret"             : "Password",               # no enable password on factory switch
        "timeout"            : 60,
        "session_timeout"    : 120,
        "global_delay_factor": 2,
        "conn_timeout"       : 20,
    }

    try:
        conn = ConnectHandler(**device)
        console.print("  [green]✓ Connected via Telnet[/green]")
    except NetmikoAuthenticationException:
        console.print(
            "  [red]✗ Authentication failed.[/red]\n"
            "  Check TARGET_DEFAULT_PASS in config.py matches "
            "the password set on vty lines (currently 'cisco')."
        )
        sys.exit(1)
    except Exception as e:
        console.print(f"  [red]✗ Telnet connection failed: {e}[/red]")
        sys.exit(1)

    # Enter enable mode — factory switch has no enable password
    try:
        conn.enable()
        console.print("  [green]✓ Entered enable mode[/green]")
    except Exception:
        # Some factory switches don't need enable
        console.print("  [yellow]  enable() skipped (not required)[/yellow]")

    # Push config
    console.print("  Pushing hostname & credentials config...")
    conn.send_config_set(
        build_config(),
        delay_factor=2,
        cmd_verify=False,
    )

# Generate RSA key for SSH
    console.print("  Generating RSA key (this takes ~30 s)...")
    conn.send_command_timing(
        "crypto key generate rsa modulus 2048",
        delay_factor=8,
        strip_prompt=False,
        strip_command=False,
    )
    # Wait generously for key generation to fully complete
    time.sleep(30)

    # Flush anything in the buffer
    conn.send_command_timing("\r\n", delay_factor=2)
    time.sleep(3)

    # Re-enter enable mode explicitly — RSA generation can drop us out
    console.print("  Re-entering enable mode before save...")
    try:
        conn.send_command_timing("enable\r\n", delay_factor=2)
        conn.send_command_timing(f"{TARGET_ENABLE}\r\n", delay_factor=2)
    except Exception:
        pass
    time.sleep(2)

    # Save using send_command_timing instead of save_config()
    # to avoid Netmiko's internal enable() check which times out
    #console.print("  Saving config...")
    #conn.send_command_timing(
   #     "write memory",
   #     delay_factor=4,
   #     strip_prompt=False,
   #     strip_command=False,
   # )
    #time.sleep(5)
   # console.print("  [green]✓ Config saved[/green]")

    # Verify hostname
    try:
        prompt = conn.find_prompt()
    except Exception:
        prompt = ""
    conn.disconnect()
    
    if TARGET_HOSTNAME in prompt:
        console.print(
            f"  [green]✓ Hostname confirmed:[/green] "
            f"{prompt.strip('#').strip('>')}"
        )
    else:
        console.print(
            f"  [yellow]⚠ Prompt is '{prompt}' — re-login to verify.[/yellow]"
        )


# ──────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────

def main():
    console.print(Panel(
        "[bold]Cisco Single-Switch Provisioner[/bold]\n"
        f"Target hostname : [cyan]{TARGET_HOSTNAME}[/cyan]\n"
        f"Provisional SW  : [cyan]{PROVISIONAL_IP}[/cyan]\n"
        f"Console port    : [cyan]{CONSOLE_PORT}[/cyan]",
        expand=False,
    ))

    # Phase 0: bypass wizard, enable Telnet + Vlan1, read IP from console
    target_ip = bypass_setup_wizard()

    # Phase 1: skipped — IP read directly from console
    console.print(
        f"\n[bold cyan]Phase 1 — Skipped "
        f"(IP read directly from console: {target_ip})[/bold cyan]"
    )

    # Phase 2: push config over Telnet
    push_config(target_ip)

    console.print(Panel(
        f"[bold green]✓ All done![/bold green]\n"
        f"Switch is now named [bold]{TARGET_HOSTNAME}[/bold]\n"
        f"SSH in with: [cyan]ssh {TARGET_NEW_USER}@{target_ip}[/cyan]",
        expand=False,
    ))


if __name__ == "__main__":
    main()
